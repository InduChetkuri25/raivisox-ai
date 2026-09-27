"""Pytest configuration for RAIVISOX tests.

Markers are registered here so pytest never emits unknown-marker warnings.
The default run (`pytest`) excludes live-LLM tests; the live suite is
selected explicitly with `pytest -m live_llm`.
"""

import os

import pytest

_LLM_PROBE_TIMEOUT = 5.0


def pytest_configure(config):
    """Configure pytest with custom markers and settings."""
    # Add custom markers
    config.addinivalue_line(
        "markers", "asyncio: mark test as async"
    )
    config.addinivalue_line(
        "markers",
        "live_llm: tests that require a running Ollama server with the "
        "qwen2.5:0.5b model (deselected by default; run with "
        "`pytest -m live_llm`)",
    )


def pytest_collection_modifyitems(config, items):
    """Modify collected test items."""
    # Automatically apply asyncio marker to async tests
    for item in items:
        if hasattr(item, "function") and hasattr(item.function, "__await__"):
            item.add_marker(pytest.mark.asyncio)

    # Default run: deselect tests marked `live_llm` unless the user explicitly
    # selects them with `-m live_llm` (CLI wins over the configured default).
    if config.getoption("-m") != "live_llm":
        for item in items:
            if item.get_closest_marker("live_llm"):
                item.add_marker(pytest.mark.skip(reason="live_llm tests need a running Ollama server (pytest -m live_llm)"))


def _ollama_api_base(base_url: str) -> str:
    """Host root for Ollama's native API.

    `RAIVISOX_LLM_BASE_URL` is an OpenAI-compatible base (…:11434/v1) used by
    the LLM client, but Ollama's native endpoints (/api/tags) live at the host
    root — probing {base}/api/tags would 404 on the /v1 prefix.
    """
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


def _ollama_reachable(base_url: str) -> bool:
    """Probe the Ollama service endpoint (no model payload)."""
    try:
        import httpx

        resp = httpx.get(f"{_ollama_api_base(base_url)}/api/tags", timeout=_LLM_PROBE_TIMEOUT)
        return resp.status_code == 200
    except Exception:
        return False


def _ollama_has_model(base_url: str, model: str) -> bool:
    """Check the model is present in the Ollama local registry."""
    try:
        import httpx

        resp = httpx.get(f"{_ollama_api_base(base_url)}/api/tags", timeout=_LLM_PROBE_TIMEOUT)
        resp.raise_for_status()
        names = [m.get("name", "") for m in resp.json().get("models", [])]
        return any(n == model or n.startswith(model + ":") for n in names)
    except Exception:
        return False


@pytest.fixture(scope="session")
def requires_ollama():
    """Provide a live Ollama session.

    Fails clearly (skip with a precise reason) unless a real Ollama server is
    reachable AND the pinned model is present. Used by live_llm tests so a
    missing model never falls back to deterministic behavior and reports
    itself as skipped rather than silently passing.
    """
    base_url = os.environ.get("RAIVISOX_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
    model = os.environ.get("RAIVISOX_LLM_MODEL", "qwen2.5:0.5b")

    if not _ollama_reachable(base_url):
        pytest.skip(
            f"live_llm requires a running Ollama server at {base_url} "
            f"(model {model}); start it with `ollama serve` and pull "
            f"`ollama pull {model}`"
        )
    if not _ollama_has_model(base_url, model):
        pytest.skip(
            f"live_llm requires model {model} to be pulled in Ollama; "
            f"found: {_ollama_models_names(base_url)}"
        )
    yield {"base_url": base_url, "model": model}


def _ollama_models_names(base_url: str):
    try:
        import httpx

        resp = httpx.get(f"{_ollama_api_base(base_url)}/api/tags", timeout=_LLM_PROBE_TIMEOUT)
        return [m.get("name", "") for m in resp.json().get("models", [])] or ["<none>"]
    except Exception:
        return ["<unreachable>"]


@pytest.fixture(scope="session")
def live_llm(requires_ollama):
    """Alias for requires_ollama — explicit `pytest -m live_llm` surface."""
    return requires_ollama
