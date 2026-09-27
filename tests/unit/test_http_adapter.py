"""Tests for the HTTP GET adapter (web_read / web_search).

Validation, capability-mapping and permission-gating tests run fully
offline. Tests marked `network` perform REAL outbound HTTP GETs to
allowlisted public hosts and require internet access.
"""

import pytest

from integrations.adapters import (
    AdapterContext,
    AdapterRegistry,
    HttpGetAdapter,
)
from integrations.adapters.base import CAPABILITY_TO_ADAPTER
from services.security.audit_trail import AuditTrail
from services.security.security_kernel import Permission, SecurityKernel


def ctx_for(actor="tester"):
    return AdapterContext(actor_id=actor, task_id="t1", step_id="s1", resource="task:t1")


@pytest.fixture
def kernel():
    k = SecurityKernel()
    k.grant_permission("tester", Permission.NETWORK_REQUEST)
    return k


@pytest.fixture
def registry(kernel):
    reg = AdapterRegistry(kernel)
    reg.register(HttpGetAdapter())
    return reg


@pytest.fixture(autouse=True)
def _clear_audit():
    AuditTrail.instance().clear()
    yield
    AuditTrail.instance().clear()


# ---------------------------------------------------------------------------
# Capability mapping & selection
# ---------------------------------------------------------------------------


def test_research_capabilities_map_to_http_get():
    assert CAPABILITY_TO_ADAPTER["web_read"] == "http_get"
    assert CAPABILITY_TO_ADAPTER["web_search"] == "http_get"


def test_select_picks_http_adapter_for_research_steps(registry):
    assert registry.select(["web_search", "web_read"]) == "http_get"
    assert registry.select(["web_read"]) == "http_get"


def test_kernel_permissions_declared():
    assert HttpGetAdapter.kernel_permissions == ["network_request"]


# ---------------------------------------------------------------------------
# URL validation (offline)
# ---------------------------------------------------------------------------


def test_allows_allowlisted_https_url(registry):
    ok, reason = registry.get("http_get")._check_url("https://en.wikipedia.org/wiki/HTTP")
    assert ok, reason


def test_rejects_non_https_scheme(registry):
    ok, reason = registry.get("http_get")._check_url("http://en.wikipedia.org/wiki/HTTP")
    assert not ok
    assert "https" in reason


def test_rejects_non_allowlisted_host(registry):
    ok, reason = registry.get("http_get")._check_url("https://evil.example.org/x")
    assert not ok
    assert "allowlist" in reason


def test_rejects_custom_port(registry):
    ok, _reason = registry.get("http_get")._check_url("https://example.com:8443/x")
    assert not ok


def test_rejects_userinfo(registry):
    ok, _reason = registry.get("http_get")._check_url("https://user:pass@example.com/x")
    assert not ok


def test_rejects_ip_literal_even_if_allowlisted():
    adapter = HttpGetAdapter(allowed_hosts=["127.0.0.1"])
    ok, reason = adapter._check_url("https://127.0.0.1/")
    assert not ok
    assert "IP-literal" in reason


def test_dns_guard_refuses_private_resolution():
    # 'localhost' would resolve to loopback — must be refused even if allowlisted
    adapter = HttpGetAdapter(allowed_hosts=["localhost"])
    ok, reason = adapter._check_url("https://localhost/")
    assert not ok
    assert "SSRF" in reason


def test_env_host_extension():
    import os

    os.environ["RAIVISOX_HTTP_ALLOWED_HOSTS"] = "docs.python.org"
    try:
        adapter = HttpGetAdapter()
        ok, _ = adapter._check_url("https://docs.python.org/3/")
        assert ok
    finally:
        del os.environ["RAIVISOX_HTTP_ALLOWED_HOSTS"]


# ---------------------------------------------------------------------------
# Permission gating & parameter handling (offline)
# ---------------------------------------------------------------------------


async def test_kernel_permission_rejection():
    kernel = SecurityKernel()  # no grants at all
    reg = AdapterRegistry(kernel)
    reg.register(HttpGetAdapter())
    r = await reg.run(
        "http_get", {"op": "read", "url": "https://example.com/"}, ctx_for(actor="netless")
    )
    assert not r.success
    assert r.permission_denied
    assert "Permission denied by security kernel" in (r.error or "")
    denials = [
        e for e in AuditTrail.instance().get_events(actor_id="netless") if not e.allowed
    ]
    assert denials, "expected a denied network_request audit event"


async def test_missing_url_param_fails_cleanly(registry):
    r = await registry.run("http_get", {"op": "read"}, ctx_for())
    assert not r.success
    assert "url" in r.error


async def test_missing_query_param_fails_cleanly(registry):
    r = await registry.run("http_get", {"op": "search"}, ctx_for())
    assert not r.success
    assert "query" in r.error


async def test_unknown_op_fails_cleanly(registry):
    r = await registry.run("http_get", {"op": "post", "url": "https://example.com/"}, ctx_for())
    assert not r.success
    assert "Unknown op" in r.error


# ---------------------------------------------------------------------------
# REAL network execution (marked `network`; skipped without internet)
# ---------------------------------------------------------------------------


@pytest.mark.network
async def test_real_web_read_example_com(registry):
    r = await registry.run(
        "http_get", {"op": "read", "url": "https://example.com/"}, ctx_for()
    )
    assert r.success, r.error
    assert r.output["status"] == 200
    assert r.output["operation"] == "web_read"
    assert "Example Domain" in r.output["content"]
    assert r.output["title"].strip().lower().startswith("example")


@pytest.mark.network
async def test_real_web_read_wikipedia(registry):
    r = await registry.run(
        "http_get",
        {
            "op": "read",
            "url": "https://en.wikipedia.org/wiki/Hypertext_Transfer_Protocol",
            "max_chars": 5000,
        },
        ctx_for(),
    )
    assert r.success, r.error
    assert r.output["status"] == 200
    assert len(r.output["content"]) > 100


@pytest.mark.network
async def test_real_web_search_returns_links(registry):
    r = await registry.run(
        "http_get", {"op": "search", "query": "HTTP status codes", "max_results": 5}, ctx_for()
    )
    assert r.success, r.error
    assert r.output["operation"] == "web_search"
    assert r.output["engine"] == "wikipedia_api"
    assert r.output["result_count"] > 0
    first = r.output["results"][0]
    assert first["title"]
    assert first["url"].startswith("https://en.wikipedia.org/wiki/")
    assert "snippet" in first


@pytest.mark.network
async def test_non_allowlisted_domain_rejected_before_any_request(registry):
    r = await registry.run(
        "http_get", {"op": "read", "url": "https://httpbin.org/get"}, ctx_for()
    )
    assert not r.success
    assert "allowlist" in r.error
    assert r.output is None
