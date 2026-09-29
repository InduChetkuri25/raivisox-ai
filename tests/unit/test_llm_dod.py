"""LLM DoD quality tests — how the LLM codegen path behaves under DoD.

The LLM HTTP transport is stubbed (deterministic, fast) while every other
stage stays REAL: the adapter's mechanical contract check, compile check,
allowlisted file write, solve-probe subprocess, the DoD subprocess probe,
the pipeline loop, VerificationEngine, and RecoveryEngine.

Live probes against the real Ollama model are run separately (live phase)
because model latency and output variance are unpredictable.

The DATABASE_URL pin below MUST run before app.main is imported: the
engine and settings are created at import time. No RAIVISOX_LLM_* env is
set here — the pipeline's singleton codegen adapter is enabled per-test
via monkeypatch so other test files keep their deterministic semantics.
"""

import asyncio
import atexit
import json
import os
import shutil
import tempfile
import uuid
from types import SimpleNamespace

import httpx
import pytest

# Pin SQLite (file-based, unique per run) BEFORE importing app.main.
_LLM_DOD_TMPDIR = tempfile.mkdtemp(prefix="raivisox_llm_dod_tests_")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{os.path.join(_LLM_DOD_TMPDIR, 'llm_dod.db')}"
atexit.register(shutil.rmtree, _LLM_DOD_TMPDIR, ignore_errors=True)

from fastapi.testclient import TestClient  # noqa: E402

from integrations.adapters import AdapterContext, CodegenAdapter  # noqa: E402
from services.dod.dod_checks import run_module_checks, summarize  # noqa: E402

# A DoD-derivable goal that does NOT match the codegen adapter's
# factorial shortcut — so the LLM engine is the one that runs.
GOAL_SUM = (
    "Create a Python module that computes the sum of all integers from 1 "
    "to 100 and verify that the result is 5050."
)
TITLE_DISTRACTOR = "Quarterly TPS Cover Sheet"

SUM_OK = (
    '"""Compute the sum of integers 1..100."""\n\n'
    "def solve():\n"
    "    total = 0\n"
    "    for i in range(1, 101):\n"
    "        total += i\n"
    "    return total\n"
)
SUM_BUGGY = (
    '"""Compute the sum of integers 1..100."""\n\n'
    "def solve():\n"
    "    total = 0\n"
    "    for i in range(1, 100):\n"
    "        total += i\n"
    "    return total\n"
)  # off-by-one: executes cleanly, returns 4950
CONTRACT_BAD = (
    '"""Interactive sum."""\n\n'
    "def solve():\n"
    "    n = input('how many? ')\n"
    "    return sum(range(1, 101))\n"
)  # violates the generation contract (input())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_llm_stub(monkeypatch, codes: list[str], per_pass: int = 3) -> list[dict]:
    """Stub the LLM HTTP transport; returns the captured request bodies.

    Responses are served in GROUPS of `per_pass` (the codegen steps of one
    pipeline pass run in parallel and share the same generation), so tests
    can script wrong-then-correct PASSES for recovery scenarios. Adapter-
    level tests make single calls, which land in the first group.
    """
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode("utf-8")))
        idx = min((len(calls) - 1) // per_pass, len(codes) - 1)
        return httpx.Response(
            200,
            json={
                "id": "stub",
                "object": "chat.completion",
                "model": "stub-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": f"```python\n{codes[idx]}\n```",
                        },
                    }
                ],
            },
        )

    class _StubClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _StubClient)
    return calls


def _enable_llm(monkeypatch, instance: CodegenAdapter) -> None:
    """Flip an adapter instance (fresh or the pipeline singleton) to LLM mode.

    Patching attributes (not env) keeps RAIVISOX_LLM_* from leaking into
    other test files, whose fallback-honesty expectations depend on it.
    """
    monkeypatch.setattr(instance, "_llm_usable", True)
    monkeypatch.setattr(instance, "llm_base_url", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(instance, "llm_model", "stub-model")
    monkeypatch.setattr(instance, "llm_api_key", "")


def _pipeline_codegen_adapter() -> CodegenAdapter:
    from app.main import adapter_registry

    return adapter_registry.get("codegen")


def _run_adapter(adapter: CodegenAdapter, params: dict) -> object:
    return asyncio.run(
        adapter.run(
            params,
            AdapterContext(actor_id="t", task_id="t1", step_id="s1", resource="t:t1/s:s1"),
        )
    )


def _audit_count(action: str, actor_id: str) -> int:
    from services.security.audit_trail import AuditTrail

    return len(AuditTrail.instance().get_events(action=action, actor_id=actor_id))


# ---------------------------------------------------------------------------
# A. Requirement 1 — the LLM receives the actual task GOAL, not the title
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    import app.tasks.router as tasks_router
    from app import main as app_main
    from app.auth import service as auth_service

    with TestClient(app_main.app) as c:
        user = SimpleNamespace(
            id=f"u-{uuid.uuid4().hex[:10]}", email=f"{uuid.uuid4().hex[:10]}@llmdod.test"
        )
        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: user
        app_main.app.dependency_overrides[tasks_router.get_current_user] = lambda: user
        app_main.app.dependency_overrides[auth_service.get_current_user] = lambda: user
        yield c
        app_main.app.dependency_overrides.clear()


def test_llm_receives_goal_not_title(client, monkeypatch):
    calls = _install_llm_stub(monkeypatch, [SUM_OK])
    _enable_llm(monkeypatch, _pipeline_codegen_adapter())

    r = client.post(
        "/api/v1/tasks",
        json={"title": TITLE_DISTRACTOR, "goal": GOAL_SUM},
    )
    assert r.status_code == 201, r.text
    task_id = r.json()["id"]

    r = client.post(f"/api/v1/tasks/{task_id}/execute", json={})
    assert r.status_code == 200, r.text
    body = r.json()

    # The LLM-generated code was correct, so DoD passed.
    assert body["status"] == "completed"
    assert body["definition_of_done"]["satisfied"] is True
    modules = body["definition_of_done"]["modules"]
    assert modules
    for m in modules:
        assert m["executed"] is True
        assert m["execution_output"] == 5050

    # Every codegen step (5, 6, 7) consulted the LLM.
    assert len(calls) == 3
    for req in calls:
        user_msg = req["messages"][1]["content"]
        assert "sum of all integers from 1 to 100" in user_msg
        assert "5050" in user_msg
        assert TITLE_DISTRACTOR not in user_msg  # the TITLE never reaches the LLM
        assert req["messages"][0]["role"] == "system"
        assert "solve" in req["messages"][0]["content"]

    # The artifacts on disk are the LLM's code — not the generic
    # deterministic fallback module (which contains a dataclass).
    from app.main import TASK_WORKSPACE

    seen = set()
    for m in modules:
        # Per-task workspace scoping: this task's own directory, not a flat
        # “tasks/generated” path (which only a stale leftover satisfied).
        path = os.path.join(TASK_WORKSPACE, task_id, "generated",
                            f"{m['module']}.py")
        assert os.path.isfile(path), path
        with open(path, encoding="utf-8") as f:
            src = f.read()
        seen.add((m["module"], "range(1, 101)" in src, "dataclass" not in src))
    for _name, has_llm_code, not_generic in seen:
        assert has_llm_code and not_generic


# ---------------------------------------------------------------------------
# B. Requirements 2 & 3 — LLM code passes DoD when correct, is rejected
#    when it executes cleanly but returns the wrong value
# ---------------------------------------------------------------------------


def test_llm_correct_code_passes_dod(monkeypatch, tmp_path):
    _install_llm_stub(monkeypatch, [SUM_OK])
    adapter = CodegenAdapter(str(tmp_path))
    _enable_llm(monkeypatch, adapter)

    result = _run_adapter(adapter, {"goal": GOAL_SUM})
    assert result.success, result.error
    out = result.output
    assert out["engine"] == "llm:stub-model"
    assert out["fallback"] is False
    assert out["compile_ok"] is True
    assert out["smoke_test"]["ran"] is True
    assert out["smoke_test"]["exit_code"] == 0
    assert result.execution_time_ms >= 0  # latency is recorded

    dod = run_module_checks(GOAL_SUM, out["written_to"])
    assert dod.executed is True
    assert dod.execution_output == 5050
    assert dod.satisfied is True
    assert summarize(GOAL_SUM, [dod])["satisfied"] is True


def test_llm_wrong_value_rejected_by_dod(monkeypatch, tmp_path):
    _install_llm_stub(monkeypatch, [SUM_BUGGY])
    adapter = CodegenAdapter(str(tmp_path))
    _enable_llm(monkeypatch, adapter)

    result = _run_adapter(adapter, {"goal": GOAL_SUM})
    # Execution SUCCEEDS — the module compiles and solve() runs cleanly.
    assert result.success, result.error
    assert result.output["engine"] == "llm:stub-model"

    # ...but the goal is NOT met: DoD rejects the real wrong value.
    dod = run_module_checks(GOAL_SUM, result.output["written_to"])
    assert dod.executed is True
    assert dod.execution_output == 4950
    assert dod.satisfied is False
    failed = [c for c in dod.checks if not c["passed"]]
    assert any(c["expected"] == 5050 and c["actual"] == 4950 for c in failed)
    summary = summarize(GOAL_SUM, [dod])
    assert summary["applicable"] is True
    assert summary["satisfied"] is False


# ---------------------------------------------------------------------------
# C. Requirement 4 — a contract violation is rejected and the fallback
#    must NOT spuriously pass DoD by echoing the goal text
# ---------------------------------------------------------------------------


def test_contract_violation_falls_back_and_dod_not_fooled(monkeypatch, tmp_path):
    _install_llm_stub(monkeypatch, [CONTRACT_BAD])
    adapter = CodegenAdapter(str(tmp_path))
    _enable_llm(monkeypatch, adapter)

    result = _run_adapter(adapter, {"goal": GOAL_SUM})
    assert result.success, result.error
    out = result.output
    assert out["fallback"] is True
    assert "contract" in (out.get("fallback_reason") or "")
    assert out["engine"] == "deterministic"

    # The fallback artifact is the generic deterministic module.
    with open(out["written_to"], encoding="utf-8") as f:
        assert "dataclass" in f.read()

    # The generic module EXECUTES (dict output) — and with the goal-echo
    # guard it must NOT satisfy the DoD just because its output restates
    # the goal (which contains "5050").
    dod = run_module_checks(GOAL_SUM, out["written_to"])
    assert dod.executed is True
    assert dod.satisfied is False
    assert summarize(GOAL_SUM, [dod])["satisfied"] is False


def test_llm_code_without_solve_falls_back(monkeypatch, tmp_path):
    """Live finding (qwen2.5:0.5b), encoded as a regression test: the model
    emits a working script with differently-named functions and no solve().
    The contract must reject it and fall back honestly — and the fallback
    module must define solve() so DoD can actually verify it.
    """
    no_solve = (
        '"""Sums 1..100 but names it differently."""\n\n'
        "def calculate_sum():\n"
        "    return sum(range(1, 101))\n\n"
        "if __name__ == '__main__':\n"
        "    print(calculate_sum())\n"
    )
    _install_llm_stub(monkeypatch, [no_solve])
    adapter = CodegenAdapter(str(tmp_path))
    _enable_llm(monkeypatch, adapter)

    result = _run_adapter(adapter, {"goal": GOAL_SUM})
    assert result.success, result.error
    out = result.output
    assert out["fallback"] is True
    assert "solve()" in (out.get("fallback_reason") or "")
    assert out["engine"] == "deterministic"

    # The fallback module HAS solve(), so the DoD probe executes it —
    # and the goal echo does NOT satisfy the expectation.
    dod = run_module_checks(GOAL_SUM, out["written_to"])
    assert dod.executed is True
    assert dod.satisfied is False


def test_goal_echo_does_not_fool_dod(tmp_path):
    """Direct DoD-level regression: an echoed goal string is not a result."""
    echo = tmp_path / "echo.py"
    echo.write_text(f"GOAL = {GOAL_SUM!r}\n\n\ndef solve():\n    return GOAL\n")
    res = run_module_checks(GOAL_SUM, str(echo))
    assert res.executed is True
    assert res.satisfied is False

    # A genuine result string still matches (the string rule is intact).
    genuine = tmp_path / "genuine.py"
    genuine.write_text("def solve():\n    return 'The sum is 5050'\n")
    res2 = run_module_checks(GOAL_SUM, str(genuine))
    assert res2.executed is True
    assert res2.satisfied is True


# ---------------------------------------------------------------------------
# D. Requirements 5 & 6 — DoD recovery regenerates an incorrect
#    LLM-generated module; completed independent nodes are not re-executed
# ---------------------------------------------------------------------------


def test_recovery_regenerates_wrong_llm_module(client, monkeypatch):
    # Attempt 1: all three codegen steps get the buggy module; after the
    # DoD failure and re-arm, the regenerated modules are correct.
    calls = _install_llm_stub(monkeypatch, [SUM_BUGGY, SUM_OK])
    _enable_llm(monkeypatch, _pipeline_codegen_adapter())

    from app.main import PIPELINE_ACTOR

    pre_fs = _audit_count("tool:fs_write", PIPELINE_ACTOR)
    pre_cg = _audit_count("tool:codegen", PIPELINE_ACTOR)

    r = client.post(
        "/api/v1/tasks",
        json={"title": TITLE_DISTRACTOR, "goal": GOAL_SUM},
    )
    assert r.status_code == 201, r.text
    task_id = r.json()["id"]

    r = client.post(f"/api/v1/tasks/{task_id}/execute", json={})
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["status"] == "completed"
    assert body["result"]["recovery_attempts"] == 1
    assert body["definition_of_done"]["satisfied"] is True
    for m in body["definition_of_done"]["modules"]:
        assert m["executed"] is True
        assert m["execution_output"] == 5050  # the regenerated value
    assert body["verification"]["verdict"] == "passed"
    for s in body["steps"]:
        assert s["status"] == "completed"

    # The LLM was genuinely re-consulted: 3 codegen steps x 2 passes.
    assert len(calls) == 6

    # Audit counts prove WHO ran: fs_write nodes (steps 4 and 11) ran
    # exactly once — never re-executed during DoD recovery — while the
    # 3 codegen nodes ran twice (regenerated).
    post_fs = _audit_count("tool:fs_write", PIPELINE_ACTOR)
    post_cg = _audit_count("tool:codegen", PIPELINE_ACTOR)
    assert post_fs - pre_fs == 2
    assert post_cg - pre_cg == 6


def test_exhausted_wrong_llm_module_fails(client, monkeypatch):
    calls = _install_llm_stub(monkeypatch, [SUM_BUGGY])  # always wrong
    _enable_llm(monkeypatch, _pipeline_codegen_adapter())

    from app.main import PIPELINE_ACTOR

    pre_cg = _audit_count("tool:codegen", PIPELINE_ACTOR)

    r = client.post(
        "/api/v1/tasks",
        json={"title": TITLE_DISTRACTOR, "goal": GOAL_SUM},
    )
    assert r.status_code == 201, r.text
    task_id = r.json()["id"]

    r = client.post(f"/api/v1/tasks/{task_id}/execute", json={})
    assert r.status_code == 200, r.text
    body = r.json()

    # Execution passed every time; the goal was never met; recovery ran
    # until exhausted and the task is FAILED — never falsely successful.
    # (No verdict assertion here: under full-suite CPU load a solve probe
    # can transiently time out and degrade the execution verdict, which
    # is not what this test verifies. The DoD/recovery/status assertions
    # below are the subject, and the audit counts prove the codegen nodes
    # really did run every pass.)
    assert body["definition_of_done"]["satisfied"] is False
    assert body["result"]["definition_of_done"]["satisfied"] is False
    assert body["status"] == "failed"
    assert body["result"]["recovery_attempts"] == 3

    # Regeneration really re-consulted the LLM each attempt:
    # 3 codegen steps x (initial + 3 recoveries) = 12 calls.
    assert len(calls) == 12
    post_cg = _audit_count("tool:codegen", PIPELINE_ACTOR)
    assert post_cg - pre_cg == 12
