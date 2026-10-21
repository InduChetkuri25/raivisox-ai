"""V3 Phase 1 acceptance tests — endpoint instrumentation (T25–T27).

No network is used: DNS validation and the transport are patched, so the
assertions are about what the adapter records, not about reachability.
"""

from __future__ import annotations

import json

import pytest

from integrations.adapters import AdapterRegistry, HttpGetAdapter
from services.graph import schema
from services.security.security_kernel import Permission, SecurityKernel
from tests.unit.v3_phase1_helpers import fetch_rows, phase1_stack  # noqa: F401
from tests.unit.v3_phase1_helpers import ctx_for


@pytest.fixture
async def stack(phase1_stack):  # noqa: F811
    return phase1_stack


@pytest.fixture(autouse=True)
def _fake_dns(monkeypatch):
    """SSRF validation resolves public hosts; keep the suite offline."""
    import integrations.adapters.http_get as http_module

    monkeypatch.setattr(http_module, "_all_ips_global", lambda host: (True, ""))


def _registry(adapter: HttpGetAdapter) -> AdapterRegistry:
    kernel = SecurityKernel()
    kernel.grant_permission("pipeline-service", Permission.NETWORK_REQUEST)
    registry = AdapterRegistry(kernel)
    registry.register(adapter)
    return registry


async def _fake_fetch(payload: str = "<html><title>T</title>body</html>", status: int = 200):
    async def _fetch(self, url, headers):
        return True, None, status, payload, {"content-type": "text/html"}

    return _fetch


# ---------------------------------------------------------------------------
# T25 — endpoint identity is normalized, not raw
# ---------------------------------------------------------------------------


async def test_allowed_endpoint_records_host_and_path_only(stack, monkeypatch):
    adapter = HttpGetAdapter()
    monkeypatch.setattr(HttpGetAdapter, "_fetch", await _fake_fetch())
    registry = _registry(adapter)

    result = await registry.run(
        "http_get",
        {"url": "https://en.wikipedia.org/wiki/HTTP?x=1&y=2"},
        ctx_for(stack),
    )
    assert result.success, result.error

    rows = await fetch_rows(stack)
    endpoint_rows = [r for r in rows if r["event_type"].startswith("endpoint.")]
    assert [r["event_type"] for r in endpoint_rows] == [
        schema.ENDPOINT_ATTEMPTED,
        schema.ENDPOINT_SUCCEEDED,
    ]
    attempted, succeeded = endpoint_rows
    assert attempted["outcome"] == schema.ATTEMPTED
    assert succeeded["outcome"] == schema.EXECUTED
    for row in (attempted, succeeded):
        assert row["target_id"] == "net:en.wikipedia.org/wiki/HTTP"
        assert row["target_kind"] == "endpoint"
        assert row["payload"]["host"] == "en.wikipedia.org"
        assert row["payload"]["path_template"] == "/wiki/HTTP"
    assert succeeded["payload"]["status"] == 200
    assert succeeded["payload"]["bytes"] == len("<html><title>T</title>body</html>")

    dumped = json.dumps(endpoint_rows, default=str)
    assert "?x=1" not in dumped
    assert "y=2" not in dumped
    # No port or userinfo in the identity (checked as host:port, since a
    # random uuid elsewhere in the row may legitimately contain "443").
    assert "en.wikipedia.org:" not in dumped
    assert "@" not in dumped


# ---------------------------------------------------------------------------
# T26 — refusals are pre-request and recorded as denials
# ---------------------------------------------------------------------------


async def test_refused_endpoint_records_denied_before_any_byte(stack, monkeypatch):
    adapter = HttpGetAdapter()
    calls: list[str] = []

    async def _spy(self, url, headers):  # pragma: no cover - must not run
        calls.append(url)
        return False, "should not be reached", 0, "", {}

    monkeypatch.setattr(HttpGetAdapter, "_fetch", _spy)
    registry = _registry(adapter)

    result = await registry.run(
        "http_get", {"url": "https://evil.example.com/steal"}, ctx_for(stack)
    )
    assert result.success is False
    assert calls == [], "no byte may be requested from a refused host"

    denied = [
        r
        for r in await fetch_rows(stack)
        if r["event_type"] == schema.ENDPOINT_DENIED
    ]
    assert len(denied) == 1
    row = denied[0]
    assert row["outcome"] == schema.DENIED
    assert row["payload"]["reason"] == "host_not_allowlisted"
    assert row["payload"]["denial_layer"] == "adapter_policy"
    assert row["payload"]["host"] == "evil.example.com"
    # No attempt/success sibling: nothing was attempted.
    assert not any(
        r["event_type"] in {schema.ENDPOINT_ATTEMPTED, schema.ENDPOINT_SUCCEEDED}
        for r in await fetch_rows(stack)
    )


# ---------------------------------------------------------------------------
# T27 — query strings (which can carry tokens) are never stored
# ---------------------------------------------------------------------------


async def test_query_string_is_not_stored(stack, monkeypatch):
    adapter = HttpGetAdapter()
    monkeypatch.setattr(HttpGetAdapter, "_fetch", await _fake_fetch())
    registry = _registry(adapter)

    result = await registry.run(
        "http_get",
        {"url": "https://en.wikipedia.org/wiki/HTTP?token=SECRET-TOKEN-VALUE"},
        ctx_for(stack),
    )
    assert result.success, result.error

    rows = await fetch_rows(stack)
    assert rows
    for row in rows:
        dumped = json.dumps(row, default=str)
        assert "SECRET-TOKEN-VALUE" not in dumped
        assert "token=" not in dumped
    assert any(r["event_type"] == schema.ENDPOINT_SUCCEEDED for r in rows)
