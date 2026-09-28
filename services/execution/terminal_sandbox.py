"""Controlled terminal execution environment.

V2 Phase 3 (docs/sandbox-design.md §7 phase 4): the historical
``create_subprocess_shell`` path is GONE. Legacy command strings are
parsed into an explicit argv representation (``shlex``), shell builtins
(``echo``/``pwd``/``cd``) are emulated deterministically without any
process, and every external program is spawned through the
ExecutionSandbox boundary — argv-only, minimal allowlisted environment
(never the API process env), working directory contained in the task
scope, bounded output, hard timeout with tree kill.

The allow/block lists remain an ADVISORY policy pre-filter with their
historical semantics (first-token allowlist, substring blocklist). They
are NOT the security boundary — the sandbox is. Shell metacharacters
(``; && | > $( )``) are ordinary data: no shell ever interprets them.

Compatibility notes (documented, deliberate):
- ``cd <dir>`` is a validated no-op, exactly as in V1 where each
  execution was a fresh shell: the directory must exist and resolve
  inside the current working directory (symlink-safe), otherwise the
  command fails like a shell ``cd`` would. The working directory does
  not persist across commands.
- ``echo``/``pwd`` are deterministic emulations (cross-platform
  identical output; no flag interpretation such as POSIX ``-e``).
- An unresolvable program fails with "command not found" semantics
  instead of a shell error page.
- Quoted arguments are parsed with POSIX shlex rules; backslash escapes
  inside double quotes are a known parsing caveat on Windows paths —
  pass such paths unquoted or use the filesystem adapters.
"""

import os
import shlex
import shutil
import sys
import time

from services.execution.sandbox import SandboxSpec, build_default_sandbox
from services.security.path_containment import contained_path

# Sentinel: the command wants the current working directory printed
# (`pwd`, or bare `cd` as both POSIX and cmd print it).
_PWD_SENTINEL = "__PWD__"


def resolve_command_argv(command: str, working_dir: str):
    """Parse a legacy command string into an explicit, shell-free form.

    Returns ``(chdir_target, argv, error)``:

    - ``chdir_target == _PWD_SENTINEL``: print the working directory
      (``pwd`` / bare ``cd``); no process needed.
    - ``chdir_target`` set to a directory: a validated ``cd <dir>`` —
      the caller applies its own persistence semantics (default none).
    - ``argv``: exec-style argument vector for an external program
      (argv[0] resolved to an absolute executable path via PATH).
    - ``error``: human-readable refusal (bad quoting, unknown program,
      cd escaping the workspace). Never a shell interpretation.

    Metacharacters are data: ``a; b`` is two argv tokens of one echo, not
    two commands, because nothing here joins them back into a shell line.
    """
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        return None, None, f"command parse error: {exc}"
    if not parts:
        return None, None, "empty command"
    prog, args = parts[0], parts[1:]

    if prog == "pwd" or (prog == "cd" and not args):
        return _PWD_SENTINEL, None, None

    if prog == "cd":
        target = args[0]
        if target.startswith("-"):
            return None, None, f"cd: unsupported option: {target}"
        newdir = os.path.abspath(os.path.join(working_dir, target))
        if not contained_path(newdir, working_dir):
            return None, None, f"cd: '{target}' resolves outside the workspace"
        if not os.path.isdir(newdir):
            return None, None, f"cd: no such directory: {target}"
        return newdir, None, None

    if prog == "echo":
        # Deterministic builtin: print argv joined by spaces + newline.
        return None, [
            sys.executable, "-c",
            "import sys; sys.stdout.write(' '.join(sys.argv[1:]) + chr(10))",
            *args,
        ], None

    exe = shutil.which(prog)
    if exe is None:
        return None, None, f"{prog}: command not found"
    return None, [exe, *args], None


class TerminalSandbox:
    """Sandboxed terminal execution: policy pre-filter + sandboxed argv.

    Every real spawn goes through ExecutionSandbox (never a shell, never
    inherited env). A sandbox failure is an honest failure in the returned
    entry — this class NEVER falls back to unsandboxed execution.
    """

    def __init__(self, working_dir: str = ".", exec_sandbox=None):
        self.working_dir = working_dir
        self._allow_list: list[str] = []
        self._block_list: list[str] = [
            "rm -rf /",
            "chmod 777 /",
            ":(){ :|:& };:",
            "mkfs.",
            "dd if=",
        ]
        self._command_history: list[dict] = []
        self._exec_sandbox = (
            exec_sandbox if exec_sandbox is not None else build_default_sandbox()
        )

    def allow_command(self, pattern: str) -> None:
        self._allow_list.append(pattern)

    def block_command(self, pattern: str) -> None:
        self._block_list.append(pattern)

    def is_allowed(self, command: str) -> tuple[bool, str]:
        cmd_lower = command.lower().strip()
        for blocked in self._block_list:
            if blocked.lower() in cmd_lower:
                return False, f"Command blocked: matches blocked pattern '{blocked}'"
        if self._allow_list:
            for allowed in self._allow_list:
                if allowed.lower() in cmd_lower:
                    return True, "Allowed"
            return False, f"Command not in allow list. Allowed patterns: {self._allow_list}"
        return True, "Allowed (no restrictions)"

    async def execute(self, command: str, timeout: int = 30) -> dict:
        """Execute a command with policy, containment, timeout, and history."""
        allowed, reason = self.is_allowed(command)
        entry = {
            "command": command,
            "allowed": allowed,
            "reason": reason,
            "timestamp": time.time(),
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "duration_ms": 0,
        }
        self._command_history.append(entry)
        _start = time.perf_counter()

        if not allowed:
            entry["stdout"] = f"BLOCKED: {reason}"
            return entry

        def _finish() -> dict:
            entry["duration_ms"] = int((time.perf_counter() - _start) * 1000)
            return entry

        cwd = os.path.abspath(self.working_dir)
        chdir_target, argv, parse_err = resolve_command_argv(command, cwd)
        if parse_err is not None:
            entry["exit_code"] = 1
            entry["stderr"] = parse_err
            return _finish()

        if chdir_target == _PWD_SENTINEL:
            entry["exit_code"] = 0
            entry["stdout"] = cwd + "\n"
            return _finish()

        if chdir_target is not None:
            # Validated `cd <dir>`: V1 shells changed nothing persistent
            # (each execution was a fresh shell); the emulation is equally
            # a validated no-op that succeeds when the directory exists.
            entry["exit_code"] = 0
            return _finish()

        spec = SandboxSpec(
            argv=argv,
            cwd=cwd,
            env={},  # sandbox builds the minimal allowlisted env
            timeout_s=float(timeout),
            workspace_writable=cwd,
        )
        result = await self._exec_sandbox.run(spec)

        if result.killed_reason == "timeout":
            entry["exit_code"] = -1
            entry["stderr"] = f"Command timed out after {timeout}s"
            return _finish()
        if result.error is not None:
            # Sandbox/spawn failure — honest failure; NEVER an unsandboxed
            # fallback execution.
            entry["stderr"] = result.error
            return _finish()

        entry["exit_code"] = result.exit_code
        entry["stdout"] = result.stdout
        entry["stderr"] = result.stderr
        entry["sandbox_enforced"] = result.enforced
        entry["sandbox_limits_hit"] = result.limits_hit
        return _finish()

    @property
    def command_history(self) -> list[dict]:
        return list(self._command_history)
