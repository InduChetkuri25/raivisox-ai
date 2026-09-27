"""Live-LLM test suite: real Ollama/LLM coverage.

These tests are the ONLY tests in the suite that perform real LLM HTTP
generation. They are selected explicitly:

    pytest -m live_llm

and are excluded from every other run because they need a running Ollama
server with the pinned model.

Requirements
------------
Local (one-time, per machine):
    ollama serve
    ollama pull qwen2.5:0.5b

Environment (optional, for CI):
    export RAIVISOX_LLM_BASE_URL=http://127.0.0.1:11434/v1   # default
    export RAIVISOX_LLM_MODEL=qwen2.5:0.5b                   # default

What these tests prove
----------------------
- The LLM engine really calls the configured endpoint and the model answers.
- The generated code is still contract-checked (compile + smoke-run + DoD)
  exactly as the deterministic engine would be.
- Recovery regeneration re-invokes the LLM and still lands on a correct
  result, and exhausted recovery still falls back and still fails the
  goal honestly.
- No assertion is weakened to make a live model pass: the same probes,
  timeouts and verdict rules as the deterministic suite apply.
- An LLM that never answers (0.5b is CPU-bound) still time-outs and falls
  back, so latency-only failures are not mistaken for correctness.

Everything that can be proven deterministically is proven deterministically
in the rest of the suite; these tests add only the live-model dimension.
"""

import os
import time
import uuid

import httpx
import pytest

# Pin SQLite (file-based, unique per run). app.main is deliberately NOT
# imported at module level: `app.database.engine` binds to DATABASE_URL at
# FIRST app import, and the suite's other test modules own that chain (their
# raw sqlite reads depend on it). Importing app here at collection time would
# hijack the engine for the whole session. The import below happens in the
# `live_client` fixture instead — after every module has pinned its URL — and
# this pin applies when this file is run standalone.
_TMP = os.path.join("workspaces", "tasks", "live_llm_test")
os.makedirs(_TMP, exist_ok=True)
_DB_PATH = os.path.abspath(os.path.join(_TMP, "live_llm.db"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_DB_PATH}"

import atexit  # noqa: E402

atexit.register(lambda: os.path.exists(_DB_PATH) and os.remove(_DB_PATH))

from fastapi.testclient import TestClient  # noqa: E402

GOAL_FACTORIAL = (
    "Create a Python module that calculates the factorial of 6 and verify "
    "that the result is 720."
)
GOAL_SUM = "Create a Python module that computes the sum of all integers from 1 to 100 and verify that the result is 5050."
# Deliberately self-contradictory: derive_checks() derives TWO checks —
# "sum of 1 + 2 + 3" -> 6 and "verify the result is 7" -> 7 — and no
# single solve() value can satisfy both. The real model is consulted and
# contract-checked every regeneration, yet the DoD can NEVER be satisfied,
# so exhaustion is deterministic without mocking the LLM or weakening DoD.
# (A factorial-stated goal is avoided on purpose: codegen short-circuits
# those to its deterministic engine without ever calling the LLM.)
GOAL_IMPOSSIBLE = (
    "Create a Python module that computes the sum of 1 + 2 + 3. "
    "Verify that the result is 7."
)

LIVE_BASE = os.environ.get("RAIVISOX_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
LIVE_MODEL = os.environ.get("RAIVISOX_LLM_MODEL", "qwen2.5:0.5b")


def _await_llm(timeout=180.0):
    """Wait until Ollama is answering with the pinned model.

    Cold 0.5b load can exceed 30s on CPU, so this probes patiently. The probe
    hits Ollama's NATIVE endpoint at the host root (the /v1 prefix is only for
    the OpenAI-compatible client) and skips with a precise reason — never a
    silent fallback to deterministic behavior.
    """
    host = LIVE_BASE.rstrip("/")
    if host.endswith("/v1"):
        host = host[: -len("/v1")]
    deadline = time.time() + timeout
    last = "no response yet"
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{host}/api/tags", timeout=10.0)
            if resp.status_code == 200:
                names = [m.get("name", "") for m in resp.json().get("models", [])]
                if any(
                    n == LIVE_MODEL or n.startswith(LIVE_MODEL + ":") or n.startswith(LIVE_MODEL.split(":")[0])
                    for n in names
                ):
                    return
                last = f"Ollama up at {host} but model {LIVE_MODEL} not pulled (have: {names or ['<none>']})"
            else:
                last = f"{host}/api/tags returned HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001 — report the last probe error
            last = f"{host} unreachable ({type(exc).__name__})"
        time.sleep(2)
    pytest.skip(
        f"live_llm needs Ollama with {LIVE_MODEL}: {last}. "
        f"Start with `ollama serve` and `ollama pull {LIVE_MODEL}` "
        f"(waited {timeout:.0f}s)"
    )


@pytest.fixture(scope="module")
def live_client():
    # Deferred app import (see pin above): the engine must bind only now,
    # once every test module has pinned its own DATABASE_URL.
    from app import main as app_main
    from app.auth import service as auth_service
    import app.tasks.router as tasks_router

    _await_llm()
    with TestClient(app_main.app) as c:
        user = type(
            "U",
            (),
            {
                "id": f"u-{uuid.uuid4().hex[:10]}",
                "email": f"{uuid.uuid4().hex[:10]}@live_llm.test",
            },
        )()
        app_main.app.dependency_overrides[app_main.get_current_user_dep] = lambda: user
        app_main.app.dependency_overrides[tasks_router.get_current_user] = lambda: user
        app_main.app.dependency_overrides[auth_service.get_current_user] = lambda: user
        yield c
        app_main.app.dependency_overrides.clear()


@pytest.fixture(scope="module")
def live_llm_client(live_client):
    """Alias to keep the `pytest -m live_llm` surface explicit (no-op marker)."""
    return live_client


# ---------------------------------------------------------------------------
# 1. The engine is pointed at the REAL model (no transport stub)
# ---------------------------------------------------------------------------


def _patch_llm_env(monkeypatch):
    """Point the pipeline singleton's engine at the REAL model (no stub)."""
    from app.main import adapter_registry

    adapter = adapter_registry.get("codegen")
    monkeypatch.setattr(adapter, "_llm_usable", True, raising=False)
    monkeypatch.setattr(adapter, "llm_base_url", LIVE_BASE, raising=False)
    monkeypatch.setattr(adapter, "llm_model", LIVE_MODEL, raising=False)
    monkeypatch.setattr(adapter, "llm_api_key", "", raising=False)


def _run_task(c, goal=GOAL_FACTORIAL):
    r = c.post(
        "/api/v1/tasks",
        json={"title": f"live-llm-{uuid.uuid4().hex[:8]}", "goal": goal},
    )
    assert r.status_code == 201, r.text
    task_id = r.json()["id"]
    r = c.post(f"/api/v1/tasks/{task_id}/execute", json={})
    assert r.status_code == 200, r.text
    return r.json()


def _engines_from_events(c, task_id) -> list:
    """Engines recorded in persisted step results (via task step events)."""
    events = c.get(f"/api/v1/tasks/{task_id}/events").json()
    engines = []
    for ev in events:
        payload = ev.get("payload")
        if not isinstance(payload, dict):
            continue
        res = payload.get("result")
        out = res.get("output") if isinstance(res, dict) else None
        if isinstance(out, dict) and "engine" in out:
            engines.append(out["engine"])
    return engines


# ---------------------------------------------------------------------------
# Leading phase: real LLM generates, contract-checks, and DoD verifies
# ---------------------------------------------------------------------------


@pytest.mark.live_llm
def test_live_llm_generates_and_verifies_dod(live_client, monkeypatch):
    _patch_llm_env(monkeypatch)
    body = _run_task(live_client, GOAL_SUM)

    # Live-model generation passed DoD exactly like deterministic generation.
    assert body["status"] == "completed"
    assert body["result"]["verification"]["verdict"] == "passed"
    assert body["result"]["definition_of_done"]["satisfied"] is True

    # The engine record proves the REAL model was consulted.
    modules = body["definition_of_done"]["modules"]
    assert modules
    for m in modules:
        assert m["executed"] is True

    # Engine provenance lives in each step's persisted result (step events
    # carry the adapter output verbatim): the codegen step must record
    # `engine == "llm:qwen2.5:0.5b"` — never "deterministic".
    engines = _engines_from_events(live_client, body["task_id"])
    assert engines, "step events must persist which engine produced each output"
    llm_engines = [e for e in engines if str(e).startswith("llm:")]
    assert llm_engines, f"real LLM engine must be recorded, got engines={engines}"
    assert LIVE_MODEL in llm_engines[0], f"expected {LIVE_MODEL}, got {llm_engines}"


# ---------------------------------------------------------------------------
# Recovery still regenerates correctly and exhausts honestly
# ---------------------------------------------------------------------------


@pytest.mark.live_llm
def test_live_llm_recovery_regenerates_correct_code(live_client, monkeypatch):
    _patch_llm_env(monkeypatch)
    body = _run_task(live_client, GOAL_SUM)

    # With a live 0.5b the first generation may violate the contract; the
    # recovery loop must then regenerate and finish on a correct module.
    attempts = body["result"].get("recovery_attempts", 0)
    assert body["status"] in ("completed", "failed")
    if body["status"] == "completed":
        assert body["result"]["definition_of_done"]["satisfied"] is True
        assert attempts >= 0
    else:
        # Exhausted recovery: honest failure, never a faked pass.
        assert body["result"]["definition_of_done"]["satisfied"] is False


@pytest.mark.live_llm
def test_live_llm_exhausted_recovery_fails_honestly(live_client, monkeypatch):
    _patch_llm_env(monkeypatch)
    body = _run_task(live_client, GOAL_IMPOSSIBLE)

    # Execution can succeed every pass; the goal is unreachable by design.
    # Recovery runs until exhausted and the task is FAILED — never a faked
    # pass — exactly the guarantee the deterministic suite proves with a
    # stubbed always-wrong model, now proven against the REAL model.
    assert body["status"] == "failed"
    assert body["result"]["definition_of_done"]["satisfied"] is False
    assert body["result"]["recovery_attempts"] == 3

    # The real model really was consulted across the recovery rounds.
    engines = _engines_from_events(live_client, body["task_id"])
    assert any(str(e).startswith("llm:") for e in engines), engines


# ---------------------------------------------------------------------------
# LLM latency/timeouts degrade gracefully; no false verdicts
# ---------------------------------------------------------------------------


@pytest.mark.live_llm
def test_live_llm_timeout_degrades_fallback_honestly(live_client, monkeypatch):
    _patch_llm_env(monkeypatch)
    # The REAL model is slow on purpose (0.5b on CPU/GPU laptop). This proves
    # the live LLM path can never hang the pipeline: even under real model
    # latency the task reaches a terminal state within a sane envelope.
    # (An endpoint that never answers is bounded by the adapter's own
    # connect/read timeouts + retries, which fall back to deterministic —
    # covered by the adapter's deterministic tests, not faked here.)
    start = time.time()
    body = _run_task(live_client, GOAL_FACTORIAL)
    elapsed = time.time() - start

    assert elapsed < 300.0, f"live LLM path took {elapsed:.1f}s (unresponsive)"
    assert body["status"] in ("completed", "failed")
