"""Builtin tool adapters — thin, honest wrappers around real tools.

Every adapter here performs a REAL operation:
- fs_read / fs_write delegate to the allowlist-enforced FileReaderTool /
  FileWriterTool (paths are additionally clamped to the adapter workspace).
- shell_exec delegates to the portable TerminalSandbox (blocklist + timeout).
- python_exec runs real Python code in a subprocess with a hard timeout.
- text_transform performs real deterministic transformations.

Capabilities without a real implementation are NOT mapped in
CAPABILITY_TO_ADAPTER and remain simulated at the pipeline layer.
"""

import asyncio
import os
import sys
from typing import Any

from integrations.adapters.base import (
    AdapterContext,
    AdapterResult,
    ToolAdapter,
    sandbox_provenance,
)
from integrations.tools.file_reader import FileReaderTool
from integrations.tools.file_writer import FileWriterTool
from services.execution.sandbox import ExecutionSandbox, SandboxSpec, build_default_sandbox
from services.execution.terminal_sandbox import TerminalSandbox
from services.execution.workspace_quota import (
    WorkspaceQuota,
    WorkspaceQuotaCache,
    WorkspaceQuotaExceeded,
)
from services.graph import events as graph_events
from services.graph import schema as graph_schema
from services.graph.provenance import ProvenanceError
from services.security.audit_trail import AuditTrail
from services.security.path_containment import contained_path


def _task_scope(workspace: str, ctx: AdapterContext | None) -> str:
    """Directory a request is clamped into for THIS task.

    SECURITY: every task gets its own directory under the shared tasks
    workspace root, so one task's filesystem operations can never read or
    write another task's artifacts. The scope is derived from the kernel
    AdapterContext — client-supplied params play no part. Without a task id
    (direct tool use outside a pipeline), the adapter workspace itself is
    the scope, exactly as before.
    """
    task_id = getattr(ctx, "task_id", None) if ctx is not None else None
    if task_id:
        safe = "".join(c for c in str(task_id) if c.isalnum() or c == "-")
        if safe:
            return os.path.join(workspace, safe)
    return workspace


def _linkage_kwargs(ctx: AdapterContext | None) -> dict[str, Any]:
    """Audit linkage from the trusted context (None when absent)."""
    return {
        "user_id": getattr(ctx, "user_id", "") or None,
        "task_id": getattr(ctx, "task_id", "") or None,
        "run_id": getattr(ctx, "execution_run_id", "") or None,
        "step_node_id": getattr(ctx, "step_id", "") or None,
        "action_id": getattr(ctx, "action_id", "") or None,
    }


def _scope_relpath(path: str, workspace: str, ctx: AdapterContext | None) -> str | None:
    """Path relative to THIS task's scope, or None when it escapes it.

    Events store scope-relative paths only (design §9); absolute host paths
    never enter the graph.
    """
    scope = _task_scope(workspace, ctx)
    try:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(scope))
    except (TypeError, ValueError):
        return None
    if os.path.isabs(rel) or rel == ".." or rel.startswith(".." + os.sep):
        return None
    return rel.replace(os.sep, "/")


def _read_error_kind(error: str | None) -> str:
    text = (error or "").lower()
    if "not found" in text:
        return "not_found"
    if "too large" in text:
        return "too_large"
    if "access denied" in text:
        return "permission"
    if "decode" in text or "encoding" in text:
        return "unreadable"
    return "unknown"


async def _record(
    ctx: AdapterContext | None,
    *,
    tool_id: str,
    event_type: str,
    outcome: str,
    payload: dict[str, Any] | None = None,
    target_kind: str | None = None,
    target_id: str | None = None,
    evidence_class: str = graph_schema.OBSERVED,
) -> None:
    """Emit an adapter-level event; recording never changes the result.

    Skipped entirely when the invocation has no registry-issued action id
    (direct adapter use outside a gate) or no provenance, so existing callers
    stay inert. ``emit`` itself counts/reports failures; only an identity
    integrity fault propagates.
    """
    if ctx is None:
        return
    if not getattr(ctx, "action_id", ""):
        return
    try:
        await graph_events.emit(
            event_type=event_type,
            outcome=outcome,
            actor_id=ctx.actor_id,
            actor_type="service",
            user_id=getattr(ctx, "user_id", "") or "",
            task_id=getattr(ctx, "task_id", "") or "",
            run_id=getattr(ctx, "execution_run_id", "") or None,
            step_node_id=getattr(ctx, "step_id", "") or None,
            tool_id=tool_id,
            action_id=ctx.action_id,
            target_kind=target_kind,
            target_id=target_id,
            evidence_class=evidence_class,
            payload=payload,
        )
    except ProvenanceError:
        raise
    except Exception:  # noqa: BLE001 - observation must not fail the action
        return


class _WorkspaceAdapter(ToolAdapter):
    """Shared workspace clamping for filesystem-backed adapters."""

    def __init__(self, workspace: str):
        self.workspace = os.path.abspath(workspace)
        os.makedirs(self.workspace, exist_ok=True)

    def _clamp(self, path: str, ctx: AdapterContext | None = None) -> str | None:
        """Resolve a request path against THIS task's scope; None if outside.

        Per-task scoping keeps concurrent tasks isolated inside the shared
        workspace root (no cross-task reads/writes). The scope directory is
        created lazily on first use.
        """
        scope = _task_scope(self.workspace, ctx)
        if scope != self.workspace:
            os.makedirs(scope, exist_ok=True)
        candidate = path if os.path.isabs(path) else os.path.join(scope, path)
        # SECURITY (Phase 0): symlink-resolving containment — a link
        # planted inside the scope must never redirect reads/writes
        # outside it. Same decision the file tools enforce at use time.
        return contained_path(candidate, scope)


class FileReadAdapter(_WorkspaceAdapter):
    """Read a real file inside the workspace via FileReaderTool."""

    id = "fs_read"
    description = "Read a file from the task workspace (allowlist-enforced)"
    kernel_permissions = ["read_files"]

    def __init__(self, workspace: str, max_bytes: int = 2 * 1024 * 1024):
        super().__init__(workspace)
        self._tool = FileReaderTool(allowed_directories=[self.workspace])
        self._max_bytes = max_bytes

    async def run(self, params: dict[str, Any], ctx: AdapterContext) -> AdapterResult:
        path = str(params.get("path", "")).strip()
        if not path:
            return AdapterResult(success=False, tool_id=self.id, error="Missing 'path' parameter")
        clamped = self._clamp(path, ctx)
        if clamped is None:
            AuditTrail.instance().log(
                actor_id=ctx.actor_id, action="tool:fs_read", resource=path,
                allowed=False, details={"reason": "path outside workspace"},
                **_linkage_kwargs(ctx),
            )
            await _record(
                ctx,
                tool_id=self.id,
                event_type=graph_schema.RESOURCE_READ_DENIED,
                outcome=graph_schema.DENIED,
                payload={
                    "reason": "outside_workspace",
                    # A basename is the most a denial may reveal (no path).
                    "requested_basename": os.path.basename(path)[:200] or None,
                },
            )
            return AdapterResult(
                success=False, tool_id=self.id,
                error=f"Access denied: '{path}' is outside the workspace",
            )
        encoding = params.get("encoding", "utf-8")
        result = await self._tool.execute({
            "path": clamped,
            "max_size": params.get("max_size", self._max_bytes),
            "encoding": encoding,
        })
        rel = _scope_relpath(clamped, self.workspace, ctx)
        task_id = getattr(ctx, "task_id", "") or ""
        target = graph_schema.resource_target(task_id, rel) if rel else None
        if result.success:
            size = (result.output or {}).get("size") if isinstance(result.output, dict) else None
            # The success entry is V3 evidence: with collection off the audit
            # stream stays byte-identical to V2 (spec §3.1 T10), so existing
            # audit counts and consumers are unaffected.
            if graph_events.events_enabled():
                AuditTrail.instance().log(
                    actor_id=ctx.actor_id, action="tool:fs_read", resource=clamped,
                    allowed=True, details={"reason": "read", "size": size},
                    **_linkage_kwargs(ctx),
                )
            payload: dict[str, Any] = {"encoding": encoding}
            if rel:
                payload["relpath"] = rel
            if size is not None:
                payload["size"] = size
            await _record(
                ctx,
                tool_id=self.id,
                event_type=graph_schema.RESOURCE_READ_OBSERVED,
                outcome=graph_schema.EXECUTED,
                payload=payload,
                target_kind="resource" if target else None,
                target_id=target,
            )
        else:
            await _record(
                ctx,
                tool_id=self.id,
                event_type=graph_schema.RESOURCE_READ_OBSERVED,
                outcome=graph_schema.FAILED,
                payload={
                    "error_kind": _read_error_kind(result.error),
                    **({"relpath": rel} if rel else {}),
                },
                target_kind="resource" if target else None,
                target_id=target,
            )
        return AdapterResult(
            success=result.success,
            tool_id=self.id,
            output=result.output,
            error=result.error,
            execution_time_ms=result.execution_time_ms,
        )


class FileWriteAdapter(_WorkspaceAdapter):
    """Write a real file inside the workspace via FileWriterTool.

    V2 closeout (T8): the write is gated by a per-scope
    :class:`WorkspaceQuota` BEFORE any bytes land — per-file size, workspace
    total, entry count, path depth, and links that resolve outside the
    scope. A refusal is an ordinary adapter failure (audited, nothing
    written), never a silent truncation of the caller's content.
    """

    id = "fs_write"
    description = "Write a file inside the task workspace (allowlist-enforced, quota-gated)"
    kernel_permissions = ["write_files"]

    def __init__(self, workspace: str, quota: WorkspaceQuota | None = None):
        super().__init__(workspace)
        self._tool = FileWriterTool(allowed_directories=[self.workspace])
        self._quota = quota if quota is not None else WorkspaceQuota.from_env()
        self._enforcers = WorkspaceQuotaCache(self._quota)

    @staticmethod
    def _payload_bytes(content: Any) -> int:
        """Exact byte size of the content about to be written."""
        if isinstance(content, (bytes, bytearray)):
            return len(content)
        if isinstance(content, str):
            return len(content.encode("utf-8"))
        if content is None:
            return 0
        # Non-string content is not what the tool's schema allows; measure
        # a conservative deterministic rendering rather than assuming zero.
        return len(str(content).encode("utf-8"))

    async def run(self, params: dict[str, Any], ctx: AdapterContext) -> AdapterResult:
        path = str(params.get("path", "")).strip()
        content = params.get("content")
        if not path or content is None:
            return AdapterResult(
                success=False, tool_id=self.id, error="Missing 'path' or 'content' parameter"
            )
        payload_bytes = self._payload_bytes(content)
        clamped = self._clamp(path, ctx)
        if clamped is None:
            AuditTrail.instance().log(
                actor_id=ctx.actor_id, action="tool:fs_write", resource=path,
                allowed=False, details={"reason": "path outside workspace"},
                **_linkage_kwargs(ctx),
            )
            await _record(
                ctx,
                tool_id=self.id,
                event_type=graph_schema.RESOURCE_WRITE_DENIED,
                outcome=graph_schema.DENIED,
                payload={
                    "reason": "outside_workspace",
                    "bytes": payload_bytes,
                    "quota_kind": None,
                },
            )
            return AdapterResult(
                success=False, tool_id=self.id,
                error=f"Access denied: '{path}' is outside the workspace",
            )
        enforcer = self._enforcers.for_scope(_task_scope(self.workspace, ctx))
        try:
            with enforcer.reserve(clamped, payload_bytes):
                result = await self._tool.execute({
                    "path": clamped,
                    "content": content,
                    "overwrite": params.get("overwrite", True),  # retries may rewrite outputs
                    "create_directories": params.get("create_directories", True),
                })
        except WorkspaceQuotaExceeded as exc:
            AuditTrail.instance().log(
                actor_id=ctx.actor_id, action="tool:fs_write", resource=path,
                allowed=False,
                details={"reason": "workspace quota", **exc.status()},
                **_linkage_kwargs(ctx),
            )
            await _record(
                ctx,
                tool_id=self.id,
                event_type=graph_schema.RESOURCE_WRITE_DENIED,
                outcome=graph_schema.DENIED,
                payload={
                    "reason": "quota",
                    "bytes": payload_bytes,
                    "quota_kind": "workspace_quota",
                },
            )
            return AdapterResult(
                success=False, tool_id=self.id,
                error=f"Workspace write refused: {exc}",
            )

        rel = _scope_relpath(clamped, self.workspace, ctx)
        task_id = getattr(ctx, "task_id", "") or ""
        target = graph_schema.resource_target(task_id, rel) if rel else None
        if result.success:
            out = result.output if isinstance(result.output, dict) else {}
            size = out.get("size", payload_bytes)
            # Same rule as fs_read: the extra success row exists only while
            # V3 collection is enabled (D11), so flag-off behaviour — and
            # every existing audit count — is unchanged.
            if graph_events.events_enabled():
                AuditTrail.instance().log(
                    actor_id=ctx.actor_id, action="tool:fs_write", resource=clamped,
                    allowed=True,
                    details={"reason": "write", "size": size},
                    **_linkage_kwargs(ctx),
                )
            committed: dict[str, Any] = {"bytes": payload_bytes}
            if rel:
                committed["relpath"] = rel
            if "created" in out:
                committed["created"] = bool(out.get("created"))
            if "overwritten" in out:
                committed["overwritten"] = bool(out.get("overwritten"))
            await _record(
                ctx,
                tool_id=self.id,
                event_type=graph_schema.RESOURCE_WRITE_COMMITTED,
                outcome=graph_schema.EXECUTED,
                payload=committed,
                target_kind="resource" if target else None,
                target_id=target,
            )
        return AdapterResult(
            success=result.success,
            tool_id=self.id,
            output=result.output,
            error=result.error,
            execution_time_ms=result.execution_time_ms,
        )


class ShellExecAdapter(ToolAdapter):
    """Execute a command via TerminalSandbox — argv through ExecutionSandbox.

    V2 Phase 3 (docs/sandbox-design.md §7 phase 4): no shell is ever
    spawned. Legacy command strings are parsed into explicit argv (shell
    builtins emulated deterministically; external programs resolved to
    absolute executable paths), the command blocklist/allowlist remains as
    the advisory policy pre-filter, and the spawn itself carries argv-only
    execution, a minimal allowlisted environment (no API-process env
    inheritance), task-scope cwd containment, bounded output, and the hard
    timeout with tree kill. A sandbox failure is an honest failure — never
    an unsandboxed fallback.
    """

    id = "shell_exec"
    description = "Run a shell command in the sandboxed terminal (blocklist + allowlist + timeout)"
    kernel_permissions = ["execute_terminal"]

    DEFAULT_ALLOW = ["python", "echo", "dir", "ls", "type", "cat", "head",
                     "tail", "wc", "sort", "findstr", "grep", "cd", "pwd", "whoami"]

    def __init__(self, workspace: str, allowed_commands: list[str] | None = None, timeout: int = 30,
                 exec_sandbox: ExecutionSandbox | None = None):
        self._root_workspace = os.path.abspath(workspace)
        # Shared ExecutionSandbox boundary (injectable for tests).
        self._exec_sandbox = exec_sandbox if exec_sandbox is not None else build_default_sandbox()
        # V2 Phase 3: policy allowlist (advisory pre-filter) + argv
        # resolution + ExecutionSandbox delegation live in TerminalSandbox.
        self._sandbox = TerminalSandbox(working_dir=self._root_workspace,
                                        exec_sandbox=self._exec_sandbox)
        for cmd in self.DEFAULT_ALLOW:
            self._sandbox.allow_command(cmd)
        for cmd in (allowed_commands or []):
            self._sandbox.allow_command(cmd)
        self._timeout = timeout
        os.makedirs(self._root_workspace, exist_ok=True)

    async def run(self, params: dict[str, Any], ctx: AdapterContext) -> AdapterResult:
        command = str(params.get("command", "")).strip()
        if not command:
            return AdapterResult(success=False, tool_id=self.id, error="Missing 'command' parameter")
        # Per-task cwd: the process works inside THIS task's scope directory
        # (shared workspace root stays task-partitioned). Relative reads and
        # writes land in the task's own directory, never another task's.
        # NOTE: TerminalSandbox has a single working_dir, but the pipeline
        # serializes per-actor execution; the scope is set immediately
        # before each execution and after computing it from the ORIGINAL
        # root (never from the previously-set scope), so no nesting occurs.
        scope = _task_scope(self._root_workspace, ctx)
        if scope != self._root_workspace:
            os.makedirs(scope, exist_ok=True)
            self._sandbox.working_dir = scope
        else:
            self._sandbox.working_dir = self._root_workspace
        allowed, reason = self._sandbox.is_allowed(command)
        if not allowed:
            AuditTrail.instance().log(
                actor_id=ctx.actor_id, action="tool:shell_exec", resource=command,
                allowed=False, details={"reason": reason},
                **_linkage_kwargs(ctx),
            )
        entry = await self._sandbox.execute(
            command,
            timeout=params.get("timeout", self._timeout),
            provenance=sandbox_provenance(ctx),
        )
        blocked_by_tool = not allowed
        return AdapterResult(
            success=(entry.get("exit_code") == 0) and not blocked_by_tool,
            tool_id=self.id,
            output={
                "command": command,
                "exit_code": entry.get("exit_code"),
                "stdout": entry.get("stdout", ""),
                "stderr": entry.get("stderr", ""),
            },
            error=None if (entry.get("exit_code") == 0 and not blocked_by_tool) else
            (reason if blocked_by_tool else (entry.get("stderr") or f"exit code {entry.get('exit_code')}")),
            execution_time_ms=entry.get("duration_ms", 0),
        )


class PythonExecAdapter(ToolAdapter):
    """Execute real Python code through the ExecutionSandbox boundary.

    V2 Phase 1 (docs/sandbox-design.md §7 phase 2): the spawn goes through
    the process-backed ExecutionSandbox instead of a bare
    create_subprocess_exec. The sandbox provides argv execution, an
    explicit minimal environment (the child no longer inherits the API
    process environment), the task-scope working directory, bounded output,
    and the hard timeout with process-tree kill — and reports honestly
    which guarantees it could actually enforce on this host.

    Still real code execution, still gated behind the kernel's
    execute_code permission; kernel authorization, workspace clamping,
    artifacts, audit, and outcomes are unchanged.
    """

    id = "python_exec"
    description = "Run Python code in a sandboxed subprocess (argv exec, minimal env, hard timeout)"
    kernel_permissions = ["execute_code"]

    def __init__(self, workspace: str, timeout: int = 20,
                 sandbox: ExecutionSandbox | None = None):
        self._workspace = os.path.abspath(workspace)
        self._timeout = timeout
        self._sandbox = sandbox if sandbox is not None else build_default_sandbox()
        os.makedirs(self._workspace, exist_ok=True)

    async def run(self, params: dict[str, Any], ctx: AdapterContext) -> AdapterResult:
        code = params.get("code")
        if not code or not isinstance(code, str):
            return AdapterResult(success=False, tool_id=self.id, error="Missing 'code' parameter")
        # Per-task cwd: untrusted code runs inside THIS task's scope
        # directory only. It can still open absolute paths by design (that
        # is real execution), but relative writes cannot cross task scopes.
        scope = _task_scope(self._workspace, ctx)
        if scope != self._workspace:
            os.makedirs(scope, exist_ok=True)
        spec = SandboxSpec(
            argv=[sys.executable, "-c", code],
            cwd=scope,
            env={},  # sandbox builds the minimal allowlisted env — no inheritance
            # Trusted identity of THIS invocation for the worker boundary.
            provenance=sandbox_provenance(ctx),
            timeout_s=float(self._timeout),
            workspace_writable=scope,
        )
        result = await self._sandbox.run(spec)
        if result.killed_reason == "timeout":
            return AdapterResult(
                success=False, tool_id=self.id,
                error=f"python_exec timed out after {int(self._timeout)}s",
                execution_time_ms=result.duration_ms,
            )
        if result.error is not None:
            # Sandbox/spawn failure — honest failure; NEVER a fallback to
            # unsandboxed execution.
            return AdapterResult(
                success=False, tool_id=self.id,
                error=result.error,
                execution_time_ms=result.duration_ms,
            )
        ok = result.exit_code == 0
        return AdapterResult(
            success=ok,
            tool_id=self.id,
            output={
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                # Additive honesty fields from the design (§4): what the
                # sandbox actually enforced for THIS run, and any limits hit.
                "sandbox_enforced": result.enforced,
                "sandbox_limits_hit": result.limits_hit,
            },
            error=None if ok else (result.stderr.strip() or f"exit code {result.exit_code}"),
            execution_time_ms=result.duration_ms,
        )


class TextTransformAdapter(ToolAdapter):
    """Deterministic, real text transformations. No privileged access."""

    id = "text_transform"
    description = "Deterministic text transformations (count/upper/lower/replace/reverse/extract_words)"
    kernel_permissions = []  # pure computation

    async def run(self, params: dict[str, Any], ctx: AdapterContext) -> AdapterResult:
        text = params.get("text")
        op = params.get("op", "count")
        if text is None or not isinstance(text, str):
            return AdapterResult(success=False, tool_id=self.id, error="Missing 'text' parameter")

        if op == "count":
            out = {"characters": len(text), "words": len(text.split()), "lines": len(text.splitlines())}
        elif op == "upper":
            out = {"text": text.upper()}
        elif op == "lower":
            out = {"text": text.lower()}
        elif op == "replace":
            old, new = params.get("find"), params.get("replacement", "")
            if old is None:
                return AdapterResult(success=False, tool_id=self.id, error="'replace' needs 'find'")
            out = {"text": text.replace(str(old), str(new))}
        elif op == "reverse":
            out = {"text": text[::-1]}
        elif op == "extract_words":
            out = {"words": text.split()}
        else:
            return AdapterResult(success=False, tool_id=self.id, error=f"Unknown transform op '{op}'")
        return AdapterResult(success=True, tool_id=self.id, output={"op": op, **out})
