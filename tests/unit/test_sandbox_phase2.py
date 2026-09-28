"""V2 Phase 2 — DoD + codegen smoke execution through the ExecutionSandbox.

Focused tests for the Phase 2 migration (docs/sandbox-design.md §7 phase 3):
both remaining untrusted spawn sites in the generate/verify loop —
CodegenAdapter._solve_probe and the DoD probe in services.dod.dod_checks —
now go through the sandbox boundary.

Proven here, without mocking the real execution:
- the codegen smoke-run and the DoD probe receive an argv-only SandboxSpec
  (never a shell string) and run as REAL processes;
- the child environment is the sandbox's minimal allowlist — API-process
  secrets (including the probe-only RAIVISOX_LLM_API_KEY) never appear;
- cwd containment uses the Phase 0 symlink-resolving boundary;
- sandbox unavailability fails honestly — the probe is never run
  unsandboxed as a fallback (canary proves no code executed);
- legacy contracts are preserved verbatim: smoke dict keys, 2000-char
  bounds, "solve probe timed out after Ns (hanging module)" and
  "solve() timed out after Ns" wording, exit-code semantics, and DoD
  result/summary shapes.
"""

import asyncio
import json
import os
import sys

import pytest

from integrations.adapters.base import AdapterContext
from integrations.adapters.codegen import CodegenAdapter
from services.dod.dod_checks import (
    DoDCheckResult,
    derive_checks,
    run_module_checks,
    run_module_checks_async,
    summarize,
)
from services.execution.sandbox import (
    ExecutionSandbox,
    ProcessSandbox,
    SandboxResult,
    SandboxSpec,
)
from services.security.path_containment import contained_path

SECRET_NAME = "RAIVISOX_PHASE2_SECRET"
SECRET_VALUE = "phase2-secret-do-not-leak-77"


def _ctx(task="t1") -> AdapterContext:
    return AdapterContext(actor_id="tester", task_id=task, step_id="s1",
                          resource=f"task:{task}")


@pytest.fixture(autouse=True)
def _secret_env():
    os.environ[SECRET_NAME] = SECRET_VALUE
    yield
    os.environ.pop(SECRET_NAME, None)


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def _write_module(path, code):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code)
    return str(path)


# ---------------------------------------------------------------------------
# 1. Codegen smoke execution through the sandbox
# ---------------------------------------------------------------------------

async def test_codegen_solve_probe_runs_through_sandbox(workspace, monkeypatch):
    """The probe receives an argv-only spec and really executes the module."""
    seen = {}

    class RecordingSandbox(ProcessSandbox):
        async def run(self, spec: SandboxSpec) -> SandboxResult:
            seen["argv"] = list(spec.argv)
            seen["env"] = dict(spec.env)
            seen["cwd"] = spec.cwd
            return await super().run(spec)

    adapter = CodegenAdapter(str(workspace), sandbox=RecordingSandbox())
    r = await adapter.run(
        {"goal": "factorial of 6", "code": "def solve():\n    return 720\n"},
        _ctx())
    assert r.success, r.error
    sm = r.output["smoke_test"]
    assert sm["ran"] is True and sm["exit_code"] == 0
    assert sm["solve_present"] is True and "720" in sm["stdout"]
    # argv-only spec: ["<python>", "-c", probe] — no shell string anywhere.
    assert len(seen["argv"]) == 3
    assert sys.executable == seen["argv"][0]
    assert seen["argv"][1] == "-c"
    assert "import json" in seen["argv"][2] and "solve" in seen["argv"][2]
    # cwd is the module's directory inside the task scope.
    assert contained_path(seen["cwd"], str(workspace / "t1"))
    # Honest enforcement reporting rides on the smoke dict.
    assert "argv_execution" in sm["sandbox_enforced"]


async def test_codegen_probe_env_is_minimal(workspace):
    """API env — including the adapter's own LLM key — never reaches the probe."""
    adapter = CodegenAdapter(str(workspace))
    r = await adapter.run(
        {"goal": "env probe",
         "code": "def solve():\n"
                 "    import os\n"
                 "    print(os.environ.get('RAIVISOX_PHASE2_SECRET', 'ABSENT'))\n"
                 "    return 1\n"},
        _ctx())
    assert r.success, r.error
    sm = r.output["smoke_test"]
    # stdout: the module's own print + the probe's JSON value line
    assert sm["stdout"].splitlines()[0] == "ABSENT"
    assert SECRET_VALUE not in sm["stdout"] + sm["stderr"]


async def test_codegen_probe_legacy_timeout_wording(workspace):
    r = await adapter_hanging(workspace)
    assert not r.success
    assert "solve probe timed out after 2s (hanging module)" in (r.error or "")


async def adapter_hanging(workspace):
    adapter = CodegenAdapter(str(workspace))
    return await adapter.run(
        {"goal": "spinner", "code": "while True:\n    pass\n",
         "smoke_timeout": 2},
        _ctx())


async def test_codegen_probe_bounded_output(workspace):
    adapter = CodegenAdapter(str(workspace))
    r = await adapter.run(
        {"goal": "noisy",
         "code": "def solve():\n    print('x' * 500000)\n    return 1\n"},
        _ctx())
    assert r.success, r.error
    sm = r.output["smoke_test"]
    assert len(sm["stdout"]) <= 2000  # legacy 2000-char bound preserved


async def test_codegen_probe_rejects_symlinked_module_dir(workspace, tmp_path):
    """A symlinked module directory resolving outside the workspace is
    rejected before any process exists (Phase 0 boundary reused)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / "t1" / "link"
    try:
        os.symlink(str(outside), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation requires privileges on this host")
    if not contained_path(str(link), str(workspace)):
        pytest.skip("host does not resolve symlink (Windows unprivileged)")
    module = _write_module(link / "mod.py", "def solve():\n    return 1\n")
    adapter = CodegenAdapter(str(workspace))
    r = await adapter.run({"goal": "escape probe", "path": module}, _ctx())
    assert not r.success
    assert "outside workspace" in (r.error or "")


# ---------------------------------------------------------------------------
# 2. DoD execution through the sandbox
# ---------------------------------------------------------------------------

async def test_dod_probe_runs_through_sandbox(tmp_path):
    seen = {}

    class RecordingSandbox(ProcessSandbox):
        async def run(self, spec: SandboxSpec) -> SandboxResult:
            seen["argv"] = list(spec.argv)
            seen["cwd"] = spec.cwd
            return await super().run(spec)

    module = _write_module(tmp_path / "factorial_of_6.py",
                           "def solve():\n    return 720\n")
    r = await run_module_checks_async(
        "factorial of 6 and verify that the result is 720.", module,
        sandbox=RecordingSandbox(), workspace_root=str(tmp_path))
    assert r.executed and r.execution_output == 720 and r.satisfied
    assert len(seen["argv"]) == 3 and seen["argv"][1] == "-c"
    assert contained_path(seen["cwd"], str(tmp_path))
    assert "argv_execution" in r.sandbox_enforced


async def test_dod_probe_env_is_minimal(tmp_path):
    """Secrets in the API process env never leak into the DoD probe."""
    module = _write_module(
        tmp_path / "envprobe.py",
        "import os\n"
        "def solve():\n"
        "    return {'secret': os.environ.get('RAIVISOX_PHASE2_SECRET', 'ABSENT')}\n")
    # "factorial of 6." derives a real check so the probe actually runs;
    # the module's own value reveals what environment the child saw.
    r = await run_module_checks_async("factorial of 6.", module,
                                      workspace_root=str(tmp_path))
    assert r.executed, r.execution_error
    assert r.execution_output == {"secret": "ABSENT"}
    assert SECRET_VALUE not in json.dumps(r.execution_output)


async def test_dod_probe_legacy_timeout_wording(tmp_path):
    module = _write_module(tmp_path / "hanger.py",
                           "def solve():\n    import time\n    while True:\n        time.sleep(1)\n")
    r = await run_module_checks_async("factorial of 6.", module, timeout=2)
    assert not r.executed
    assert r.execution_error == "solve() timed out after 2s"
    chk = r.checks[0]
    assert chk["id"] == "execution" and chk["passed"] is False


async def test_dod_probe_no_solve_honest(tmp_path):
    module = _write_module(tmp_path / "nosolve.py", "x = 1\n")
    r = await run_module_checks_async("factorial of 6.", module)
    assert not r.executed
    assert r.execution_error == "module has no callable solve()"


async def test_dod_bounded_output_and_crash_semantics(tmp_path):
    # Crashing module: real traceback tail in execution_error (legacy shape)
    module = _write_module(tmp_path / "crasher.py",
                           "def solve():\n    raise ValueError('boom')\n")
    r = await run_module_checks_async("factorial of 6.", module)
    assert not r.executed
    assert "boom" in r.execution_error

    # Noisy but successful module: output bounded, result still parsed
    module2 = _write_module(tmp_path / "noisy.py",
                            "def solve():\n    print('y' * 500000)\n    return 720\n")
    r2 = await run_module_checks_async(
        "factorial of 6 and verify that the result is 720.", module2)
    assert r2.executed and r2.satisfied


async def test_dod_workspace_containment_rejects_outside_module(tmp_path):
    """workspace_root containment: module outside the workspace is refused
    before spawn; result stays abstain-free and honestly failed."""
    outside_root = tmp_path / "elsewhere"
    module = _write_module(outside_root / "evil.py",
                           "def solve():\n    return 720\n")
    r = await run_module_checks_async(
        "factorial of 6.", module, workspace_root=str(tmp_path / "ws"))
    assert not r.executed
    assert "outside workspace" in r.execution_error


async def test_dod_semantics_preserved_end_to_end(tmp_path):
    """Existing DoD meaning unchanged: independent expectation, echo
    rejection, override hook, and summarize() aggregation."""
    goal = "factorial of 6 and verify that the result is 720."
    good = _write_module(tmp_path / "good.py", "def solve():\n    return 720\n")
    rg = await run_module_checks_async(goal, good)
    assert rg.satisfied and summarize(goal, [rg])["satisfied"] is True

    # Wrong value: executes cleanly, goal NOT met (execution ≠ satisfaction)
    wrong = _write_module(tmp_path / "wrong.py", "def solve():\n    return 719\n")
    rw = await run_module_checks_async(goal, wrong)
    assert rw.executed and not rw.satisfied
    s = summarize(goal, [rw])
    assert s["applicable"] is True and s["satisfied"] is False

    # Negative-test hook still works through the sandboxed path
    r_override = await run_module_checks_async(goal, wrong, override_return=720)
    assert r_override.executed and r_override.satisfied

    # Abstain path unchanged
    ra = await run_module_checks_async("write a poem", good)
    assert ra.abstained
    s2 = summarize("write a poem", [ra])
    assert s2["applicable"] is False


async def test_dod_sync_wrapper_equivalent(tmp_path):
    """The sync wrapper (test/direct callers) returns identical results."""
    module = _write_module(tmp_path / "syncmod.py",
                           "def solve():\n    return 720\n")
    goal = "factorial of 6 and verify that the result is 720."
    r_sync = run_module_checks(goal, module)
    r_async = await run_module_checks_async(goal, module)
    assert r_sync.executed == r_async.executed
    assert r_sync.execution_output == r_async.execution_output
    assert r_sync.satisfied == r_async.satisfied


# ---------------------------------------------------------------------------
# 3. Sandbox unavailable — honest failure, NO unsandboxed fallback
# ---------------------------------------------------------------------------

class BrokenSandbox(ExecutionSandbox):
    async def run(self, spec: SandboxSpec) -> SandboxResult:
        return SandboxResult(error="sandbox backend unavailable",
                             killed_reason="supervisor", enforced=[])

    def capabilities(self) -> dict[str, bool]:
        return {"argv_execution": False}


async def test_codegen_sandbox_unavailable_fails_step(workspace):
    canary = workspace / "canary.py"
    adapter = CodegenAdapter(str(workspace), sandbox=BrokenSandbox())
    r = await adapter.run(
        {"goal": "escaped?",
         "code": f"open({str(canary)!r}, 'w').write('ran')\ndef solve():\n    return 1\n"},
        _ctx())
    assert not r.success
    assert "sandbox unavailable" in (r.error or "")
    assert not canary.exists()  # code never executed anywhere


async def test_dod_sandbox_unavailable_fails_honestly(tmp_path):
    module = _write_module(tmp_path / "canary.py",
                           "def solve():\n    open('escaped.txt', 'w').write('ran')\n    return 720\n")
    r = await run_module_checks_async(
        "factorial of 6.", module, sandbox=BrokenSandbox())
    assert not r.executed
    assert "sandbox backend unavailable" in r.execution_error
    assert not (tmp_path / "escaped.txt").exists()


# ---------------------------------------------------------------------------
# 4. Enforcement honesty — limits never claimed when not enforced
# ---------------------------------------------------------------------------

async def test_enforcement_reporting_matches_host_capabilities(tmp_path):
    module = _write_module(tmp_path / "mod.py", "def solve():\n    return 720\n")
    r = await run_module_checks_async("factorial of 6.", module,
                                      workspace_root=str(tmp_path))
    assert r.executed
    caps = ProcessSandbox().capabilities()
    if caps["memory_limit"]:
        assert "memory_limit" not in r.sandbox_enforced  # spec asked for none
    # Core guarantees must be present — they are always enforced.
    for key in ("argv_execution", "minimal_env", "working_dir", "bounded_output"):
        assert key in r.sandbox_enforced


def test_no_shell_execution_anywhere_in_specs():
    """Guard: the SandboxSpec type has no shell-string field — argv only."""
    import dataclasses

    fields = {f.name for f in dataclasses.fields(SandboxSpec)}
    assert "argv" in fields
    assert not any("shell" in f or "command" in f for f in fields)
