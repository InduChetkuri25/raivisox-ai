"""Code generation adapter — writes real, runnable Python modules.

What is REAL here:
- Every generated module is WRITTEN to a real file in the task workspace
  (allowlist-enforced FileWriterTool) and then COMPILE-CHECKED with
  `compile(source, path, "exec")`.
- Every module is SMOKE-RUN in a subprocess (`python -c "import module"`)
  inside the workspace before the step reports success — a module that
  does not import is a failed step, not a "generated" one.
- The deterministic engine composes a real, self-contained Python module
  from the step's goal text: header docstring derived from the goal and a
  `solve(...)` core. Nothing is random.
- The optional LLM engine (same env config as text_intel) generates the
  module code when configured; on failure the adapter falls back to the
  deterministic engine and says so.

Honesty contract: output records `engine`, `written_to`, `compile_ok`,
`smoke_test` (with real stdout/exit code), and `fallback`. A module is
reported generated only if it was written AND compiled AND smoke-ran.
"""

import asyncio
import os
import re
import sys
import time
from typing import Any

from integrations.adapters.base import AdapterContext, AdapterResult, ToolAdapter
from integrations.tools.file_writer import FileWriterTool
from services.execution.sandbox import ExecutionSandbox, SandboxSpec, build_default_sandbox
from services.security.audit_trail import AuditTrail
from services.security.path_containment import contained_path

_LLM_TIMEOUT_SECONDS = 120.0  # cold-start model load can exceed 30s on CPU
_LLM_CONNECT_TIMEOUT = 5


def _task_scope(workspace: str, ctx: object) -> str:
    """Directory a request is clamped into for THIS task.

    SECURITY: tasks are isolated into per-task directories under the shared
    workspace root so one task's generated code/artifacts can never collide
    with — or be read by — another task. Derived from the kernel
    AdapterContext (trusted); without a task id the adapter workspace is
    the scope (direct tool use behaves exactly as before).
    """
    task_id = getattr(ctx, "task_id", None) if ctx is not None else None
    if task_id:
        safe = "".join(c for c in str(task_id) if c.isalnum() or c == "-")
        if safe:
            return os.path.join(workspace, safe)
    return workspace


_MAX_LLM_RETRIES = 2
_MAX_PROMPT_CHARS = 12_000
_MAX_COMPLETION_CHARS = 12_000
# Repair rounds for a contract-violating generation. Each round re-prompts
# with the SPECIFIC mechanical violation (never a weakened check). Evidence
# (qwen2.5:0.5b, 2026-09): single-shot 2/8 contract-compliant; +repair
# rounds 7/8 — and the one residual failure still falls back honestly.
_LLM_REPAIR_ROUNDS = 2

# Strict, contract-first generation prompt. A fully worked EXAMPLE format
# anchors small models far better than prose requirements alone (0.5b
# frequently ignored the old prose-only contract entirely).
_LLM_SYSTEM_PROMPT = (
    "Write one complete Python module.\n"
    "Rules:\n"
    '1. First line: a docstring """...""" describing the module.\n'
    "2. Define a function exactly like this: def solve():\n"
    "3. solve() must compute the answer to the goal and return the result "
    "(an int or str).\n"
    "4. No parameters on solve.\n"
    "5. Last lines:\n"
    "if __name__ == '__main__':\n"
    "    print(solve())\n"
    "6. Standard library only. No input(), no files, no network.\n"
    "7. Output ONLY a ```python code block. Nothing else.\n"
    "\n"
    "Example format:\n"
    "```python\n"
    '"""Demo module."""\n\n\n'
    "def solve():\n"
    "    return 42\n\n\n"
    "if __name__ == '__main__':\n"
    "    print(solve())\n"
    "```\n"
    "Goal: {goal}\n"
)

_LLM_REPAIR_PROMPT = (
    "Your previous answer did not satisfy the required module contract.\n"
    "Problem: {problem}\n"
    "Write the complete corrected module now. Output ONLY a ```python code block.\n"
    "Goal: {goal}\n"
)

# Module names to avoid — they shadow stdlib modules and would make the
# generated file a hazard when imported by other pipeline artifacts.
_RESERVED_NAMES = frozenset({
    "json", "os", "sys", "re", "time", "math", "random", "string",
    "test", "tests", "types", "typing", "datetime", "collections",
    "itertools", "functools", "logging", "pathlib", "shutil", "subprocess",
})

# Mechanical enforcement of the generation contract: generated modules are
# self-contained computations — no interactivity, no I/O, no process or
# network spawning. (Lesson from a live run: the model emitted an
# interactive input() menu despite the prompt, which hangs any executor.)
_BLOCKED_CODE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\binput\s*\(", "interactive input() is not allowed"),
    (r"\beval\s*\(", "eval() is not allowed"),
    (r"\bexec\s*\(", "exec() is not allowed"),
    (r"\b__import__\s*\(", "__import__() is not allowed"),
    (r"\bos\.system|\bos\.popen\b", "os process spawning is not allowed"),
    (r"\b(subprocess|socket|asyncio|multiprocessing)\b", "process/network modules are not allowed"),
    (r"\b(urllib|requests|http\.client|httpx)\b", "network modules are not allowed"),
    (r"\bopen\s*\(", "file I/O is not allowed"),
)


def _slug(text: str) -> str:
    """Lowercase letter/digit words from goal text, space-joined."""
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _module_name(goal: str, fallback: str = "generated_module") -> str:
    """Derive a safe Python module name from the goal text."""
    words = re.findall(r"[a-z0-9]+", (goal or "").lower())
    stop = {"a", "an", "the", "to", "for", "of", "and", "with", "in", "on",
            "that", "this", "module", "python", "build", "create", "make",
            "write", "implement", "application", "app"}
    words = [w for w in words if w not in stop]
    if not words:
        return fallback
    name = "_".join(words[:4])[:40].strip("_")
    if not name:
        return fallback
    if name[0].isdigit():
        name = f"mod_{name}"
    if name in _RESERVED_NAMES:
        name = f"{name}_module"
    return name


_FACTORIAL_GOAL_RE = re.compile(
    r"factorial\s+(?:of\s+|for\s+)?(\d{1,3})", re.IGNORECASE
)


def _factorial_module(n: int) -> str:
    """A REAL factorial module for goals like 'factorial of 6'.

    solve() computes n! with a genuine iterative algorithm and returns the
    integer — exactly the shape the DoD checks service verifies against an
    independently computed expectation (math.factorial(n)).
    """
    return (
        f'"""Compute the factorial of {n}.\n\n'
        "Generated by the RAIVISOX codegen adapter (deterministic engine).\n"
        '"""\n\n'
        f"N = {n}\n\n\n"
        "def solve() -> int:\n"
        f'    """Return {n}! computed iteratively."""\n'
        "    result = 1\n"
        "    for i in range(2, N + 1):\n"
        "        result *= i\n"
        "    return result\n\n\n"
        "if __name__ == '__main__':\n"
        "    print(solve())\n"
    )


def _extract_code(text: str) -> str:
    """Pull Python source from an LLM reply (```python fences or raw)."""
    text = (text or "").strip()
    fence = re.search(r"```(?:python|py)?[ \t]*\r?\n(.*?)```", text, re.DOTALL)
    if fence:
        return fence.group(1).strip() + "\n"
    return text + "\n"


class CodegenAdapter(ToolAdapter):
    """Generate, write, compile-check, and smoke-run a real Python module.

    Params:
      goal (required, non-empty str): what the module should do. The
        deterministic engine derives the module name and skeleton from it;
        the LLM engine receives it as the generation prompt.
      path (optional): workspace-relative .py path
        (default: generated/<module_name>.py, name derived from goal).
      code (optional): caller-supplied module source — skips generation
        (still compiled, written, and smoke-run; nothing is trusted
        blindly).
      smoke_test (optional, default True): import the module in a
        subprocess and require exit code 0.
      title (optional): human label recorded in output.
    """

    id = "codegen"
    description = (
        "Code generation: writes a real Python module to the workspace, "
        "compile-checks it, and smoke-runs it in a subprocess. LLM engine "
        "when configured, deterministic engine otherwise."
    )
    # Writes files AND spawns interpreter subprocesses — needs both grants.
    kernel_permissions: list[str] = ["write_files", "execute_code"]

    def __init__(self, workspace: str, smoke_timeout: int = 20,
                 sandbox: ExecutionSandbox | None = None):
        self.workspace = os.path.abspath(workspace)
        os.makedirs(self.workspace, exist_ok=True)
        self._writer = FileWriterTool(allowed_directories=[self.workspace])
        self._smoke_timeout = smoke_timeout
        # V2 Phase 2: the smoke-run subprocess goes through the
        # ExecutionSandbox boundary (argv-only, minimal env, timeout,
        # bounded output) instead of a bare create_subprocess_exec.
        self._sandbox = sandbox if sandbox is not None else build_default_sandbox()
        self.llm_api_key = (
            os.environ.get("RAIVISOX_LLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        )
        self.llm_base_url = (
            os.environ.get("RAIVISOX_LLM_BASE_URL") or "https://api.openai.com/v1"
        ).rstrip("/")
        self.llm_model = os.environ.get("RAIVISOX_LLM_MODEL") or "gpt-4o-mini"
        self._llm_usable = bool(self.llm_api_key) or "localhost" in self.llm_base_url or "127.0.0.1" in self.llm_base_url

    # ------------------------------------------------------------------
    # Optional LLM engine (real OpenAI-compatible call)
    # ------------------------------------------------------------------

    async def _llm_generate(self, goal: str) -> tuple[bool, str, str]:
        """Returns (ok, code_or_error, model).

        Generation loop: initial attempt plus up to _LLM_REPAIR_ROUNDS
        repair rounds. A repair round is triggered ONLY by a mechanical
        structural-contract violation (no callable solve(), or the module
        does not compile) and re-prompts with that exact problem — the
        validation is never weakened, and the security ban patterns stay
        enforced by _contract_check in run() regardless. Transient HTTP
        errors keep their separate retry budget.
        """
        import httpx

        goal = (goal or "").strip()[:_MAX_PROMPT_CHARS]
        headers = {"Content-Type": "application/json"}
        if self.llm_api_key:
            headers["Authorization"] = f"Bearer {self.llm_api_key}"
        last_err = ""
        base_messages = [
            {"role": "system", "content": _LLM_SYSTEM_PROMPT.format(goal=goal)},
            {"role": "user", "content": goal},
        ]
        messages = base_messages
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_LLM_TIMEOUT_SECONDS, connect=_LLM_CONNECT_TIMEOUT)
        ) as client:
            for round_no in range(_LLM_REPAIR_ROUNDS + 1):
                payload = {
                    "model": self.llm_model,
                    "messages": messages,
                    "max_tokens": 2000,
                    "temperature": 0.2,
                }
                text = None
                # Transient-error retry budget (separate from repair rounds).
                for _attempt in range(_MAX_LLM_RETRIES + 1):
                    try:
                        resp = await client.post(
                            f"{self.llm_base_url}/chat/completions",
                            json=payload,
                            headers=headers,
                        )
                        if resp.status_code in (429, 500, 502, 503, 504):
                            last_err = f"LLM HTTP {resp.status_code}"
                            await asyncio.sleep(0.5)
                            continue
                        if resp.status_code != 200:
                            return False, f"LLM HTTP {resp.status_code}: {resp.text[:200]}", self.llm_model
                        data = resp.json()
                        text = data["choices"][0]["message"]["content"]
                        model = data.get("model", self.llm_model)
                        code = _extract_code(text or "")
                        if not code.strip():
                            last_err = "LLM returned empty code"
                            break
                        # Mechanical structural contract check. The security
                        # ban patterns are NOT checked here — they remain
                        # enforced by _contract_check in run() on whatever
                        # this loop finally accepts.
                        if not re.search(r"^def\s+solve\s*\(", code, re.M):
                            last_err = (
                                "module does not define a callable function "
                                "named solve()"
                            )
                            break
                        try:
                            compile(code, "<generated>", "exec")
                        except SyntaxError as exc:
                            last_err = f"syntax error: {exc.msg}"
                            break
                        # Security/contract ban patterns also trigger a
                        # repair round (with the exact violation named).
                        violations = self._contract_check(code)
                        if violations:
                            last_err = (
                                "LLM code violated contract: "
                                + "; ".join(violations)
                            )
                            break
                        return True, code, model
                    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
                        detail = str(exc) or repr(exc)
                        last_err = f"LLM call failed: {type(exc).__name__}: {detail}"
                        await asyncio.sleep(0.5)
                else:
                    # Retries exhausted on transient errors — stop entirely.
                    break
                if round_no < _LLM_REPAIR_ROUNDS:
                    messages = base_messages + [
                        {"role": "assistant", "content": (text or "")[:_MAX_COMPLETION_CHARS]},
                        {
                            "role": "user",
                            "content": _LLM_REPAIR_PROMPT.format(
                                problem=last_err or "the module did not satisfy the contract",
                                goal=goal,
                            ),
                        },
                    ]
        return False, last_err, self.llm_model

    # ------------------------------------------------------------------
    # Deterministic engine — a real, self-contained module from the goal
    # ------------------------------------------------------------------

    def _deterministic_module(self, goal: str) -> str:
        goal_one_line = " ".join(_slug(goal).split()[:24]) or "pipeline task"
        words = _slug(goal).split()
        def camel(ws: list[str]) -> str:
            return "".join(w.capitalize() for w in ws[:3]) or "Task"
        class_name = camel([w for w in words if w not in
                            {"a", "an", "the", "to", "for", "of", "and", "with"}] or ["task"])
        return (
            f'"""{goal_one_line}.\n\n'
            "Generated by the RAIVISOX codegen adapter (deterministic engine).\n"
            '"""\n\n'
            "from dataclasses import dataclass\n\n\n"
            f"@dataclass\nclass {class_name}:\n"
            f'    """Core problem representation for: {goal_one_line}."""\n\n'
            "    payload: str\n\n"
            "    def key_terms(self, limit: int = 5) -> list[str]:\n"
            '        stop = {"the", "a", "an", "of", "and", "to", "for", "with"}\n'
            "        seen: list[str] = []\n"
            "        for term in self.payload.lower().split():\n"
            "            term = term.strip('.,:;!?()')\n"
            "            if len(term) > 2 and term not in stop and term not in seen:\n"
            "                seen.append(term)\n"
            "            if len(seen) >= limit:\n"
            "                break\n"
            "        return seen\n\n\n"
            f"def solve(payload: str = {goal_one_line!r}) -> dict:\n"
            f'    """Solve: {goal_one_line}."""\n'
            f"    task = {class_name}(payload=payload)\n"
            "    return {\n"
            '        "goal": payload,\n'
            '        "key_terms": task.key_terms(),\n'
            '        "payload_length": len(payload),\n'
            "    }\n\n\n"
            "if __name__ == '__main__':\n"
            "    import json\n\n"
            f"    print(json.dumps(solve()))\n"
        )

    # ------------------------------------------------------------------
    # Real verification: compile check + subprocess smoke run
    # ------------------------------------------------------------------

    @staticmethod
    def _compile_check(code: str, filename: str) -> str | None:
        try:
            compile(code, filename, "exec")
            return None
        except SyntaxError as exc:
            return f"SyntaxError: {exc.msg} (line {exc.lineno})"

    @staticmethod
    def _contract_check(code: str) -> list[str]:
        """Mechanically enforce the generation contract on model output.

        The deterministic engine never violates this; the LLM engine
        sometimes does (observed live: an interactive input() menu). A
        violation is a generation failure, not a runtime surprise.
        """
        violations = []
        for pattern, reason in _BLOCKED_CODE_PATTERNS:
            if re.search(pattern, code):
                violations.append(reason)
        # The contract requires a callable solve() — the pipeline's solve
        # probe and the Definition-of-Done stage both depend on it. Live
        # evidence (qwen2.5:0.5b): the model happily emits a working
        # script with differently-named functions and no solve() at all;
        # accepting it wastes an execution cycle on an unverifiable
        # artifact. A violation falls back honestly to the deterministic
        # engine, whose modules always define solve().
        if not re.search(r"\bdef\s+solve\s*\(", code):
            violations.append("module does not define solve()")
        return violations

    async def _solve_probe(
        self, module_stem: str, module_dir: str, timeout: int | None = None
    ) -> dict[str, Any]:
        """Import the module AND execute solve() in a sandboxed subprocess.

        V2 Phase 2 (docs/sandbox-design.md §7 phase 3): the spawn goes
        through the ExecutionSandbox — argv-only, minimal allowlisted env
        (no API-process inheritance), cwd contained in the module
        directory, bounded output, hard timeout with tree kill. A hanging
        module (interactive loop, endless sleep) fails the step instead of
        hanging the pipeline. A sandbox failure is an honest failure — the
        probe is NEVER run unsandboxed as a fallback.
        """
        # Phase 0 containment: the module directory must resolve inside
        # this adapter's workspace (a symlinked module dir pointing
        # outside is rejected before any process exists).
        if not contained_path(module_dir, self.workspace):
            return {"ran": False, "exit_code": None, "stdout": "",
                    "stderr": "sandbox rejected spec: module directory "
                              "outside workspace",
                    "mode": "solve_probe", "sandbox_enforced": []}
        probe_code = (
            f"import json, {module_stem}\n"
            f"f = getattr({module_stem}, 'solve', None)\n"
            f"print(json.dumps(f()) if callable(f) else 'no-solve')\n"
        )
        effective_timeout = timeout or self._smoke_timeout
        spec = SandboxSpec(
            argv=[sys.executable, "-c", probe_code],
            cwd=module_dir,
            env={},  # sandbox builds the minimal allowlisted env
            timeout_s=float(effective_timeout),
            # Generous but bounded: stdout[:2000] is taken downstream, and
            # heavy stdout must not crowd out the probe's own result line
            # before that truncation (pre-sandbox capture was unbounded).
            output_limit_bytes=2_000_000,
            workspace_writable=module_dir,
        )
        result = await self._sandbox.run(spec)
        if result.killed_reason == "timeout":
            return {
                "ran": True, "exit_code": None, "stdout": "",
                "stderr": f"solve probe timed out after {effective_timeout}s "
                          "(hanging module)",
                "mode": "solve_probe",
                "sandbox_enforced": result.enforced,
            }
        if result.error is not None:
            # Sandbox/spawn failure: honest failure, no unsandboxed retry.
            return {"ran": False, "exit_code": None, "stdout": "",
                    "stderr": result.error, "mode": "solve_probe",
                    "sandbox_enforced": result.enforced}
        out_text = result.stdout[:2000]
        return {
            "ran": True,
            "exit_code": result.exit_code,
            "stdout": out_text,
            "stderr": result.stderr[:2000],
            "solve_present": out_text.strip() != "no-solve",
            "mode": "solve_probe",
            "sandbox_enforced": result.enforced,
        }

    # ------------------------------------------------------------------

    def _clamp(self, path: str) -> str | None:
        # SECURITY (Phase 0): symlink-resolving containment — a link
        # planted inside the workspace must never redirect generated
        # module writes outside it.
        return contained_path(path, self.workspace)

    async def run(self, params: dict[str, Any], ctx: AdapterContext) -> AdapterResult:
        started = time.perf_counter()

        def _done(success: bool, output=None, error: str | None = None) -> AdapterResult:
            return AdapterResult(
                success=success,
                tool_id=self.id,
                output=output,
                error=error,
                execution_time_ms=int((time.perf_counter() - started) * 1000),
            )

        goal = params.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            return _done(False, error="codegen requires a non-empty 'goal' parameter")

        module_name = _module_name(goal)
        path = str(params.get("path", "")).strip() or f"generated/{module_name}.py"
        if not path.endswith(".py"):
            path += ".py"

        # ---- 1. Obtain source (caller-supplied > LLM > deterministic) --
        engine = "caller"
        fallback = False
        fallback_reason = None
        # Goals that state an exactly-verifiable computation (e.g.
        # "factorial of 6") get a real implementation from the
        # deterministic engine — one whose output the DoD checks service
        # can independently verify. The LLM engine stays available for
        # everything else.
        _det_goal = None
        _m_fact = _FACTORIAL_GOAL_RE.search(goal)
        if _m_fact:
            _det_goal = _factorial_module(int(_m_fact.group(1)))
        code = params.get("code")
        if isinstance(code, str) and code.strip():
            code = code if code.endswith("\n") else code + "\n"
        else:
            code = None
        if code is None and _det_goal is not None:
            engine = "deterministic"
            code = _det_goal
        if code is None:
            if self._llm_usable:
                ok, result, model = await self._llm_generate(goal.strip())
                if ok:
                    # Mechanically enforce the contract BEFORE accepting
                    # model output — a violation falls back honestly.
                    violations = self._contract_check(result)
                    if violations:
                        fallback_reason = (
                            f"LLM code violated contract: {'; '.join(violations)}"
                        )
                    else:
                        engine = f"llm:{model}"
                        code = result
                else:
                    fallback_reason = result
            else:
                fallback_reason = "no LLM backend configured"
            if code is None:
                # fallback=True ONLY when the LLM was actually tried and
                # failed — never when no backend was configured at all.
                fallback = self._llm_usable
                engine = "deterministic"
                code = self._deterministic_module(goal.strip())

        # ---- 2. Compile check (real) -----------------------------------
        compile_err = self._compile_check(code, os.path.basename(path))
        compile_ok = compile_err is None

        # ---- 3. Write the real file (allowlist-enforced) ----------------
        scope = _task_scope(self.workspace, ctx)
        if scope != self.workspace:
            os.makedirs(scope, exist_ok=True)
            path = os.path.join(scope, path)
        clamped = self._clamp(path)
        if clamped is None:
            AuditTrail.instance().log(
                actor_id=ctx.actor_id, action="tool:codegen", resource=path,
                allowed=False, details={"reason": "path outside workspace"},
            )
            return _done(False, error=f"Access denied: '{path}' is outside the workspace")
        write_result = await self._writer.execute({
            "path": clamped,
            "content": code,
            "overwrite": True,  # retries legitimately regenerate the module
            "create_directories": True,
        })
        if not write_result.success:
            return _done(False, error=write_result.error,
                         output={"engine": engine, "compile_ok": compile_ok,
                                 "fallback": fallback, "fallback_reason": fallback_reason})

        # ---- 4. Solve probe (real subprocess execution) -----------------
        written_to = (write_result.output or {}).get("path", clamped)
        module_stem = os.path.splitext(os.path.basename(path))[0]
        # Invalidate stale bytecode before any probe imports the module.
        # A .pyc header records the source mtime at 1-second granularity:
        # a recovery rewrite of the same-length module within the same
        # wall-clock second leaves an entry Python considers fresh, and
        # the probes would silently execute the PREVIOUS pass's code —
        # making regeneration ineffective. (Caught by the LLM DoD tests.)
        _pyc_dir = os.path.join(os.path.dirname(os.path.abspath(written_to)), "__pycache__")
        try:
            for _f in os.listdir(_pyc_dir):
                if _f.startswith(module_stem + "."):
                    os.unlink(os.path.join(_pyc_dir, _f))
        except OSError:
            pass
        smoke = await self._solve_probe(
            module_stem,
            os.path.dirname(os.path.abspath(written_to)),
            timeout=int(params["smoke_timeout"]) if params.get("smoke_timeout") else None,
        )
        smoke["module"] = module_stem
        output = {
            "operation": "generate_module",
            "engine": engine,
            "fallback": fallback,
            "module": module_stem,
            "written_to": written_to,
            "bytes": (write_result.output or {}).get("size"),
            "compile_ok": compile_ok,
            "compile_error": compile_err,
            "smoke_test": smoke,
            "source_preview": code[:400],
        }
        if fallback_reason:
            output["fallback_reason"] = fallback_reason

        # A module is only "generated" if it compiles AND executes.
        problems = []
        if not compile_ok:
            problems.append(compile_err)
        if not smoke.get("ran"):
            # Sandbox/spawn failure — honest failure; never silently treat
            # an unverified module as generated, and NEVER fall back to an
            # unsandboxed probe.
            problems.append(f"sandbox unavailable: {smoke.get('stderr', '')[:300]}")
        elif smoke.get("exit_code") != 0:
            problems.append(f"solve probe failed: {smoke.get('stderr', '')[:300]}")
        if problems:
            return _done(False, output=output, error="; ".join(problems))
        return _done(True, output=output)
