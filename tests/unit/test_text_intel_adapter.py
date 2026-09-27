"""Tests for the text-intelligence adapter (understand / plan / analyze / generate).

The deterministic-engine tests assert REAL properties: outputs are derived
from the input text (no fabricated sentences), error patterns classify
correctly, and engine labels are honest. LLM-path tests stub the transport
and assert request formation, response parsing, and honest fallback.
"""

import json

import httpx
import pytest

import integrations.adapters.text_intel as ti
from integrations.adapters import (
    AdapterContext,
    AdapterRegistry,
    TextIntelAdapter,
)
from integrations.adapters.base import CAPABILITY_TO_ADAPTER
from services.security.security_kernel import SecurityKernel


def ctx_for(actor="tester"):
    return AdapterContext(actor_id=actor, task_id="t1", step_id="s1", resource="task:t1")


SAMPLE = (
    "Migrate the payment service to the new API gateway. "
    "The service must handle 500 requests per second. "
    "We should complete the migration within two weeks. "
    "Do not touch the production database during the migration. "
    "The credentials for the old gateway are stored in vault. "
    "How do we verify that no requests are dropped?"
)


@pytest.fixture
def adapter():
    a = TextIntelAdapter()
    a._llm_usable = False  # deterministic engine under test
    return a


@pytest.fixture
def registry():
    reg = AdapterRegistry(SecurityKernel())
    reg.register(TextIntelAdapter())
    reg.get("text_intel")._llm_usable = False
    return reg


# ---------------------------------------------------------------------------
# Capability mapping & selection
# ---------------------------------------------------------------------------


def test_reasoning_capabilities_map_to_text_intel():
    assert CAPABILITY_TO_ADAPTER["intent_parsing"] == "text_intel"
    assert CAPABILITY_TO_ADAPTER["planning"] == "text_intel"
    assert CAPABILITY_TO_ADAPTER["document_generation"] == "text_intel"
    assert CAPABILITY_TO_ADAPTER["error_analysis"] == "text_intel"


def test_code_read_and_data_parse_are_not_remapped():
    # code_read/data_parse steps must keep resolving to fs_read/python_exec
    assert "code_read" not in CAPABILITY_TO_ADAPTER
    assert "data_parse" not in CAPABILITY_TO_ADAPTER


def test_select_picks_text_intel(registry):
    assert registry.select(["intent_parsing"]) == "text_intel"
    assert registry.select(["planning", "document_generation"]) == "text_intel"


# ---------------------------------------------------------------------------
# Deterministic engine — real operations, derived from the input
# ---------------------------------------------------------------------------


async def test_analyze_derives_output_from_input(adapter):
    r = await adapter.run({"op": "analyze", "text": SAMPLE}, ctx_for())
    assert r.success, r.error
    out = r.output
    assert out["engine"] == "deterministic"
    assert out["goal"].startswith("Migrate the payment service")
    reqs = " ".join(out["requirements"]).lower()
    assert "must" in reqs or "should" in reqs
    assert any(
        "production" in s.lower() or "credential" in s.lower() or "vault" in s.lower()
        for s in out["risks"]
    )
    assert out["open_questions"], "the question sentence must be detected"


async def test_summarize_never_fabricates_sentences(adapter):
    """Core honesty invariant: every summary sentence comes from the input."""
    r = await adapter.run({"op": "summarize", "text": SAMPLE}, ctx_for())
    assert r.success, r.error
    src = [s.strip().lower() for s in ti._sentences(SAMPLE)]
    for sent in ti._sentences(r.output["summary"]):
        assert sent.strip().lower() in src, f"fabricated sentence: {sent!r}"


async def test_plan_produces_ordered_steps(adapter):
    r = await adapter.run({"op": "plan", "text": SAMPLE}, ctx_for())
    assert r.success, r.error
    out = r.output
    assert out["step_count"] == len(out["plan"]) == 5
    assert [s["order"] for s in out["plan"]] == [1, 2, 3, 4, 5]
    assert out["plan"][3]["kind"] == "verify"
    assert out["key_topics"]


async def test_analyze_error_classifies_module_not_found(adapter):
    err = "Traceback: ModuleNotFoundError: No module named 'httpx'"
    r = await adapter.run({"op": "analyze_error", "text": err}, ctx_for())
    assert r.success, r.error
    out = r.output
    assert out["root_cause"] == "missing module/package"
    assert "httpx" in out["remediation"]
    assert out["pattern_matched"]


async def test_analyze_error_unclassified_is_honest(adapter):
    r = await adapter.run({"op": "analyze_error", "text": "weird failure xyz"}, ctx_for())
    assert r.success
    assert r.output["root_cause"] == "unclassified error"
    assert not r.output["pattern_matched"]


async def test_generate_builds_document_from_source(adapter):
    r = await adapter.run({"op": "generate", "text": SAMPLE}, ctx_for())
    assert r.success, r.error
    out = r.output
    assert out["document"].startswith("# Summary")
    assert out["characters"] == len(out["document"])
    assert "Migrate" in out["document"]  # derived from input, not invented


async def test_missing_text_fails_cleanly(adapter):
    r = await adapter.run({"op": "analyze"}, ctx_for())
    assert not r.success
    assert "text" in r.error


async def test_unknown_op_fails_cleanly(adapter):
    r = await adapter.run({"op": "dream", "text": SAMPLE}, ctx_for())
    assert not r.success
    assert "Unknown op" in r.error


# ---------------------------------------------------------------------------
# LLM engine path (stubbed transport — request/response contract)
# ---------------------------------------------------------------------------


async def test_llm_engine_used_when_configured(adapter, monkeypatch):
    adapter._llm_usable = True

    async def fake_complete(op, text):
        return True, "LLM analysis result", "test-model-1"

    monkeypatch.setattr(adapter, "_llm_complete", fake_complete)
    r = await adapter.run({"op": "analyze", "text": SAMPLE}, ctx_for())
    assert r.success
    assert r.output["engine"] == "llm:test-model-1"
    assert r.output["fallback"] is False


async def test_falls_back_to_deterministic_when_llm_fails(adapter, monkeypatch):
    adapter._llm_usable = True

    async def fail_complete(op, text):
        return False, "LLM HTTP 503", "test-model-1"

    monkeypatch.setattr(adapter, "_llm_complete", fail_complete)
    r = await adapter.run({"op": "plan", "text": SAMPLE}, ctx_for())
    assert r.success, "deterministic fallback must still succeed"
    out = r.output
    assert out["engine"] == "deterministic"
    assert out["fallback"] is True
    assert out["fallback_reason"] == "LLM HTTP 503"
    assert out["step_count"] == 5


async def test_llm_complete_parses_openai_response(adapter, monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        body = json.loads(request.content)
        captured["model"] = body["model"]
        captured["messages"] = body["messages"]
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [{"message": {"content": "hello from the mock"}}],
            },
        )

    adapter._llm_usable = True
    adapter.llm_api_key = "sk-test"
    real_client = httpx.AsyncClient

    def factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(ti.httpx, "AsyncClient", factory)
    ok, text, model = await adapter._llm_complete("summarize", "sum this")
    assert ok and text == "hello from the mock" and model == "gpt-4o-mini"
    assert captured["url"].endswith("/chat/completions")
    assert captured["auth"] == "Bearer sk-test"
    assert captured["messages"][1]["content"] == "sum this"


# ---------------------------------------------------------------------------
# Configuration & security
# ---------------------------------------------------------------------------


def test_local_base_url_enables_llm_engine(monkeypatch):
    monkeypatch.setenv("RAIVISOX_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    a = TextIntelAdapter()
    assert a._llm_usable is True
    assert a.llm_base_url == "http://127.0.0.1:11434/v1"


def test_no_backend_means_deterministic(monkeypatch):
    monkeypatch.delenv("RAIVISOX_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("RAIVISOX_LLM_BASE_URL", "https://api.openai.com/v1")
    a = TextIntelAdapter()
    assert a._llm_usable is False


async def test_runs_without_kernel_permissions():
    """Text intelligence is unprivileged: no grants required to run."""
    reg = AdapterRegistry(SecurityKernel())  # no grants at all
    reg.register(TextIntelAdapter())
    reg.get("text_intel")._llm_usable = False
    r = await reg.run("text_intel", {"op": "summarize", "text": SAMPLE}, ctx_for(actor="bare"))
    assert r.success, r.error
    assert not r.permission_denied
