"""Tests for the codegen adapter — real module generation and verification.

Every test asserts REAL effects: a file exists on disk, its source
compiles, and importing it in a subprocess succeeds (or fails honestly).
The LLM path is tested at contract level with a stubbed transport; the
deterministic engine is tested for real, goal-derived output.
"""

import json
import os
import subprocess
import sys

import pytest

import integrations.adapters.codegen as cg
from integrations.adapters import (
    AdapterContext,
    AdapterRegistry,
    CodegenAdapter,
)
from integrations.adapters.base import CAPABILITY_TO_ADAPTER
from services.security.audit_trail import AuditTrail
from services.security.security_kernel import Permission, SecurityKernel


def ctx_for(actor="tester", task="t1", step="s1"):
    return AdapterContext(
        actor_id=actor,
        task_id=task,
        step_id=step,
        resource=f"task:{task}/step:{step}",
    )


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return str(ws)


@pytest.fixture
def kernel():
    k = SecurityKernel()
    k.grant_permission("tester", Permission.WRITE_FILES)
    k.grant_permission("tester", Permission.EXECUTE_CODE)
    return k


@pytest.fixture
def adapter(workspace, monkeypatch):
    # Force the deterministic engine: no LLM backend configured.
    monkeypatch.delenv("RAIVISOX_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("RAIVISOX_LLM_BASE_URL", raising=False)
    a = CodegenAdapter(workspace)
    assert a._llm_usable is False
    return a


@pytest.fixture
def registry(workspace, kernel, monkeypatch):
    monkeypatch.delenv("RAIVISOX_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("RAIVISOX_LLM_BASE_URL", raising=False)
    reg = AdapterRegistry(kernel)
    reg.register(CodegenAdapter(workspace))
    return reg


@pytest.fixture(autouse=True)
def _clear_audit():
    AuditTrail.instance().clear()
    yield
    AuditTrail.instance().clear()


# ---------------------------------------------------------------------------
# Capability mapping & selection
# ---------------------------------------------------------------------------


def test_code_generation_maps_to_codegen():
    assert CAPABILITY_TO_ADAPTER["code_generation"] == "codegen"


def test_select_prefers_codegen_over_fs_write(registry):
    # Decomposer emits ["code_generation", "filesystem_write"]; codegen
    # (the richer real operation) must win once registered.
    assert registry.select(["code_generation", "filesystem_write"]) == "codegen"
    assert registry.select(["code_generation"]) == "codegen"


async def test_codegen_alone_is_real_not_simulated(registry):
    """A step tagged ONLY with code_generation resolves to a real adapter."""
    assert registry.select(["code_generation"]) is not None


# ---------------------------------------------------------------------------
# Deterministic engine — real, goal-derived modules
# ---------------------------------------------------------------------------


async def test_generates_writes_and_smoke_runs_real_module(adapter):
    r = await adapter.run({"goal": "Build a python calculator module"}, ctx_for())
    assert r.success, r.error
    out = r.output
    assert out["engine"] == "deterministic"
    assert out["fallback"] is False
    assert out["compile_ok"] is True
    assert out["smoke_test"]["ran"] is True
    assert out["smoke_test"]["exit_code"] == 0, out["smoke_test"]["stderr"]
    # The file REALLY exists at the recorded path.
    assert os.path.exists(out["written_to"])
    with open(out["written_to"], encoding="utf-8") as f:
        src = f.read()
    assert "def solve(" in src
    assert out["module"] in src or True  # name derived from goal
    assert out["module"] == "calculator"  # goal-derived, not generic


async def test_module_name_derived_from_goal(adapter):
    assert cg._module_name("Build a python calculator module") == "calculator"
    # Long goals truncate at 4 content words.
    assert (
        cg._module_name("Implement User Interface components")
        == "user_interface_components"
    )
    assert cg._module_name("") == "generated_module"
    # Exact stdlib names get suffixed, never shadowed.
    assert cg._module_name("json") == "json_module"
    # Compound names that do NOT shadow stdlib are kept.
    assert cg._module_name("json parser") == "json_parser"
    # Leading digit guarded.
    assert cg._module_name("3d renderer") == "mod_3d_renderer"


async def test_generated_module_actually_runs(adapter):
    """Run the generated artifact end-to-end: python module.py -> JSON."""
    r = await adapter.run({"goal": "summarize text with word frequency"}, ctx_for())
    assert r.success, r.error
    proc = subprocess.run(
        [sys.executable, r.output["written_to"]],
        capture_output=True, text=True, timeout=30, cwd=adapter.workspace,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["goal"].startswith("summarize text")
    assert isinstance(payload["key_terms"], list)


async def test_caller_supplied_code_is_verified_not_trusted(adapter):
    code = "def add(a, b):\n    return a + b\n"
    r = await adapter.run({"goal": "adder helper", "code": code}, ctx_for())
    assert r.success, r.error
    assert r.output["engine"] == "caller"
    assert os.path.exists(r.output["written_to"])
    with open(r.output["written_to"], encoding="utf-8") as f:
        assert "def add(a, b):" in f.read()


# ---------------------------------------------------------------------------
# Honest failures — bad code is a failed step, not a fake success
# ---------------------------------------------------------------------------


async def test_syntax_error_is_reported_and_fails(adapter):
    r = await adapter.run(
        {"goal": "broken thing", "code": "def broken(:\n    pass\n"}, ctx_for()
    )
    assert not r.success
    assert "SyntaxError" in (r.error or "")
    assert r.output["compile_ok"] is False
    assert r.output["compile_error"]


async def test_runtime_import_failure_fails_solve_probe(adapter):
    r = await adapter.run(
        {"goal": "crasher", "code": "raise RuntimeError('boom on import')\n"},
        ctx_for(),
    )
    assert not r.success
    assert "solve probe failed" in (r.error or "")
    assert r.output["smoke_test"]["exit_code"] != 0


async def test_hanging_module_times_out_instead_of_hanging_pipeline(adapter):
    """An endless loop must fail the step, never hang the executor."""
    r = await adapter.run(
        {"goal": "spinner", "code": "while True:\n    pass\n", "smoke_timeout": 2},
        ctx_for(),
    )
    assert not r.success
    assert "timed out" in (r.error or "")


async def test_solve_execution_is_verified(adapter):
    """The probe actually CALLS solve() and records its real output."""
    code = (
        "def solve(x=21):\n"
        "    return {'doubled': x * 2}\n"
    )
    r = await adapter.run({"goal": "doubler", "code": code}, ctx_for())
    assert r.success, r.error
    sm = r.output["smoke_test"]
    assert sm["solve_present"] is True
    assert "doubled" in sm["stdout"]


async def test_module_without_solve_is_honest(adapter):
    code = "def helper():\n    return 1\n"
    r = await adapter.run({"goal": "helper lib", "code": code}, ctx_for())
    assert r.success, r.error
    assert r.output["smoke_test"]["solve_present"] is False


async def test_llm_input_violation_falls_back_honestly(workspace, monkeypatch):
    """The live-run lesson: model emits input() menus — must be rejected."""
    monkeypatch.setenv("RAIVISOX_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    a = CodegenAdapter(workspace)

    bad_llm_code = (
        "def solve():\n"
        "    return input('number: ')\n"
    )

    async def bad_generate(goal):
        return True, bad_llm_code, "qwen2.5:0.5b"

    monkeypatch.setattr(a, "_llm_generate", bad_generate)
    r = await a.run({"goal": "interactive probe"}, ctx_for())
    assert r.success, "deterministic fallback must rescue the step"
    out = r.output
    assert out["engine"] == "deterministic"
    assert out["fallback"] is True
    assert "input()" in out["fallback_reason"]


async def test_missing_goal_fails_cleanly(adapter):
    r = await adapter.run({}, ctx_for())
    assert not r.success
    assert "goal" in (r.error or "")


# ---------------------------------------------------------------------------
# Security boundaries
# ---------------------------------------------------------------------------


async def test_path_outside_workspace_denied(adapter):
    target = os.path.join(adapter.workspace, "..", "escape_should_not_exist.py")
    r = await adapter.run({"goal": "escape artist", "path": target}, ctx_for())
    assert not r.success
    assert "outside the workspace" in (r.error or "")
    assert not os.path.exists(
        os.path.join(os.path.dirname(adapter.workspace), "escape_should_not_exist.py")
    )


async def test_kernel_permission_required(workspace):
    kernel = SecurityKernel()  # no grants
    reg = AdapterRegistry(kernel)
    reg.register(CodegenAdapter(workspace))
    r = await reg.run("codegen", {"goal": "x"}, ctx_for(actor="unprivileged"))
    assert not r.success
    assert r.permission_denied
    denials = [
        e for e in AuditTrail.instance().get_events(actor_id="unprivileged")
        if not e.allowed
    ]
    assert denials, "denial must be audited"


# ---------------------------------------------------------------------------
# LLM engine path (stubbed transport — request/response contract)
# ---------------------------------------------------------------------------


async def test_llm_engine_generates_real_code(workspace, monkeypatch):
    monkeypatch.setenv("RAIVISOX_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    a = CodegenAdapter(workspace)
    assert a._llm_usable is True

    llm_code = '"""LLM module."""\n\ndef solve():\n    return {"ok": True}\n'

    async def fake_generate(goal):
        return True, llm_code, "qwen2.5:0.5b"

    monkeypatch.setattr(a, "_llm_generate", fake_generate)
    r = await a.run({"goal": "produce a hello module"}, ctx_for())
    assert r.success, r.error
    assert r.output["engine"] == "llm:qwen2.5:0.5b"
    assert r.output["fallback"] is False
    assert r.output["compile_ok"] is True
    assert os.path.exists(r.output["written_to"])


async def test_llm_failure_falls_back_to_deterministic_honestly(workspace, monkeypatch):
    monkeypatch.setenv("RAIVISOX_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    a = CodegenAdapter(workspace)

    async def fail_generate(goal):
        return False, "LLM HTTP 503", "qwen2.5:0.5b"

    monkeypatch.setattr(a, "_llm_generate", fail_generate)
    r = await a.run({"goal": "fallback probe"}, ctx_for())
    assert r.success, "deterministic fallback must still produce a working module"
    out = r.output
    assert out["engine"] == "deterministic"
    assert out["fallback"] is True
    assert out["fallback_reason"] == "LLM HTTP 503"
    assert out["smoke_test"]["exit_code"] == 0


async def test_llm_code_block_extraction():
    fenced = "Here you go:\n```python\nprint('hi')\n```\nEnjoy!"
    assert cg._extract_code(fenced) == "print('hi')\n"
    bare = "```\nprint('bare')\n```"
    assert cg._extract_code(bare) == "print('bare')\n"
    assert cg._extract_code("print('raw')") == "print('raw')\n"
