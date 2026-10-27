"""Tests for the HTTP GET adapter (web_read / web_search).

Two layers, with an explicit execution policy:

* Everything without a marker runs fully offline. Response handling is pinned
  against a controlled transport and an answered DNS consult, so the parsing,
  refusal and gating paths are proven without depending on anyone's uptime.
* Tests marked ``network`` are the ONLY evidence about public DNS, real TLS and
  Internet reachability, and nothing else in this file may claim those. They run
  by default (CI does not deselect the marker), so they are written to fail only
  on something this repository owns: no connectivity skips with a stated reason,
  a connection/TLS failure fails, a fabricated success fails — but a third
  party's own 4xx/5xx is reported as that status and does not fail the suite.
  CI is therefore never red because example.com or Wikipedia had a bad minute.
"""

import re

import httpx
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
# Response handling against a controlled transport (offline)
# ---------------------------------------------------------------------------
#
# Only the socket and the resolver are substituted. Production accepts https on
# the scheme's default port only and refuses every non-global resolved address,
# so a loopback server is unreachable by design — a local fixture would have to
# weaken the guard it is supposed to test. What still runs for real: the kernel
# gate in AdapterRegistry, the allowlist, the per-hop redirect re-validation, the
# status/content-type/size handling, `max_chars`, and the HTML/JSON parsing.
# These tests say nothing about public DNS, TLS or reachability; the `network`
# section below is the only place that can, and it is labelled as such.

_HTML_PAGE = b"".join(
    [
        b"<html><head><title>Example Domain</title></head><body>",
        b"<h1>Example Domain</h1><p>This domain is for use in documentation "
        b"examples without needing permission.</p></body></html>",
    ]
)


def _stub_public_dns(monkeypatch, allowed: bool = True):
    """Answer the SSRF/DNS consult; return the hosts it was asked about."""
    import integrations.adapters.http_get as http_get

    calls: list[str] = []

    def _all_ips_global(host: str):
        calls.append(host)
        if allowed:
            return True, ""
        return False, f"Refusing non-public resolved address for host '{host}' (SSRF guard)"

    monkeypatch.setattr(http_get, "_all_ips_global", _all_ips_global)
    return calls


def _http_transport(monkeypatch, respond):
    """Point the adapter's real httpx client at ``respond``; return requested URLs."""
    import integrations.adapters.http_get as http_get

    seen: list[str] = []
    real_client = httpx.AsyncClient

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return respond(request)

    transport = httpx.MockTransport(_handler)

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(http_get.httpx, "AsyncClient", _client)
    return seen


async def test_web_read_returns_the_parsed_response(registry, monkeypatch):
    """Status, title, body and bytes come from the actual response payload."""
    dns = _stub_public_dns(monkeypatch)
    seen = _http_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "text/html; charset=utf-8"}, content=_HTML_PAGE
        ),
    )

    r = await registry.run(
        "http_get", {"op": "read", "url": "https://example.com/"}, ctx_for()
    )
    assert r.success, r.error
    assert r.output["status"] == 200
    assert r.output["operation"] == "web_read"
    assert r.output["url"] == "https://example.com/"
    assert r.output["title"] == "Example Domain"
    assert "documentation examples" in r.output["content"]
    assert r.output["bytes"] == len(r.output["content"])
    # The allowlisted name did not skip the guard: DNS was consulted for it.
    assert dns and set(dns) == {"example.com"}, dns
    assert seen == ["https://example.com/"]


async def test_web_read_honours_max_chars(registry, monkeypatch):
    """`max_chars` truncates the returned body but not the reported size."""
    _stub_public_dns(monkeypatch)
    _http_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "text/html"}, content=_HTML_PAGE
        ),
    )

    r = await registry.run(
        "http_get",
        {"op": "read", "url": "https://example.com/", "max_chars": 40},
        ctx_for(),
    )
    assert r.success, r.error
    assert len(r.output["content"]) == 40
    assert r.output["bytes"] == len(_HTML_PAGE)
    assert r.output["bytes"] > len(r.output["content"])


async def test_web_read_non_200_is_a_failure(registry, monkeypatch):
    """A remote error status is surfaced verbatim, never as success or content."""
    _stub_public_dns(monkeypatch)
    _http_transport(
        monkeypatch, lambda request: httpx.Response(503, text="service unavailable")
    )

    r = await registry.run(
        "http_get", {"op": "read", "url": "https://example.com/"}, ctx_for()
    )
    assert not r.success
    assert r.output is None
    assert "HTTP 503" in (r.error or "")


async def test_web_read_refuses_non_text_content(registry, monkeypatch):
    """A 200 alone is not enough; binary payloads are refused."""
    _stub_public_dns(monkeypatch)
    _http_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "application/octet-stream"}, content=b"\x00" * 64
        ),
    )

    r = await registry.run(
        "http_get", {"op": "read", "url": "https://example.com/"}, ctx_for()
    )
    assert not r.success
    assert "non-text" in (r.error or "").lower()


async def test_web_read_refuses_a_redirect_out_of_the_allowlist(registry, monkeypatch):
    """An approved origin cannot redirect the adapter to an unapproved host."""
    _stub_public_dns(monkeypatch)
    seen = _http_transport(
        monkeypatch,
        lambda request: httpx.Response(
            302, headers={"location": "https://evil.example.org/steal"}
        ),
    )

    r = await registry.run(
        "http_get", {"op": "read", "url": "https://example.com/"}, ctx_for()
    )
    assert not r.success
    assert "allowlist" in (r.error or "").lower()
    assert seen == ["https://example.com/"], "the redirect target must never be fetched"


async def test_non_allowlisted_domain_rejected_before_any_request(registry, monkeypatch):
    """An unlisted host is refused with nothing sent on the wire."""
    _stub_public_dns(monkeypatch)
    seen = _http_transport(
        monkeypatch, lambda request: httpx.Response(200, text="must not be sent")
    )

    r = await registry.run(
        "http_get", {"op": "read", "url": "https://httpbin.org/get"}, ctx_for()
    )
    assert not r.success
    assert "allowlist" in r.error
    assert r.output is None
    assert seen == [], "a non-allowlisted host must not reach the network layer"


async def test_web_search_parses_the_api_payload(registry, monkeypatch):
    """Query encoding, result shape and HTML-stripped snippets come from JSON."""
    _stub_public_dns(monkeypatch)
    payload = {
        "query": {
            "searchinfo": {"totalhits": 42},
            "search": [
                {"title": "HTTP 404", "snippet": '<span class="m">Not</span> Found'},
                {"title": "HTTP 503", "snippet": "Service Unavailable"},
            ],
        }
    }
    seen = _http_transport(monkeypatch, lambda request: httpx.Response(200, json=payload))

    r = await registry.run(
        "http_get",
        {"op": "search", "query": "HTTP status codes", "max_results": 2},
        ctx_for(),
    )
    assert r.success, r.error
    out = r.output
    assert out["operation"] == "web_search"
    assert out["engine"] == "wikipedia_api"
    assert out["result_count"] == 2
    assert out["total_hits"] == 42
    assert out["results"][0]["title"] == "HTTP 404"
    assert out["results"][0]["url"] == "https://en.wikipedia.org/wiki/HTTP_404"
    assert out["results"][0]["snippet"] == "Not Found"
    assert out["results"][1]["url"] == "https://en.wikipedia.org/wiki/HTTP_503"
    assert len(seen) == 1
    assert seen[0].startswith("https://en.wikipedia.org/w/api.php?action=query")
    assert "srsearch=HTTP%20status%20codes" in seen[0]
    assert "srlimit=2" in seen[0]


async def test_web_search_clamps_result_limit(registry, monkeypatch):
    """An oversized `max_results` is clamped to the documented ceiling."""
    _stub_public_dns(monkeypatch)
    seen = _http_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json={"query": {"search": []}}),
    )

    r = await registry.run(
        "http_get", {"op": "search", "query": "x", "max_results": 999}, ctx_for()
    )
    assert r.success, r.error
    assert r.output["result_count"] == 0
    assert "srlimit=20" in seen[0]


async def test_web_search_reports_invalid_json(registry, monkeypatch):
    """A non-JSON body is a clean failure, not an empty result set."""
    _stub_public_dns(monkeypatch)
    _http_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "text/plain"}, text="<html>not json</html>"
        ),
    )

    r = await registry.run("http_get", {"op": "search", "query": "x"}, ctx_for())
    assert not r.success
    assert "invalid JSON" in (r.error or "")


async def test_web_search_non_200_is_a_failure(registry, monkeypatch):
    """A remote error status on the search endpoint is reported, not parsed."""
    _stub_public_dns(monkeypatch)
    _http_transport(monkeypatch, lambda request: httpx.Response(429, text="slow down"))

    r = await registry.run("http_get", {"op": "search", "query": "x"}, ctx_for())
    assert not r.success
    assert r.output is None
    assert "HTTP 429" in (r.error or "")


# ---------------------------------------------------------------------------
# LIVE network execution (marked `network`)
# ---------------------------------------------------------------------------
#
# These are the only tests here that can speak to public DNS, real TLS and
# Internet reachability, and they are deliberately thin: the behaviour they used
# to confuse with a third party's health (parsing, refusals, limits) is pinned
# above. A live read must either succeed with a 200 or report the far end's own
# status verbatim; anything else — a fabricated success, a failure with no HTTP
# verdict, a TLS/connection break — fails. No connectivity skips with a reason.


def _reachable(host: str, port: int = 443) -> bool:
    """TCP reachability probe — not a status check on someone else's page."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=5):
            return True
    except OSError:
        return False


def _assert_honest_read(result, url: str) -> None:
    """Either a 200 with output, or the server's own status and no output."""
    if result.success:
        assert result.output["status"] == 200
        return
    error = result.error or ""
    match = re.fullmatch(rf"HTTP (\d{{3}}) from {re.escape(url)}", error)
    assert match, f"live read produced no HTTP verdict: {error!r}"
    assert 400 <= int(match.group(1)) <= 599
    assert result.output is None


@pytest.mark.network
async def test_real_web_read_example_com(registry):
    if not _reachable("example.com"):
        pytest.skip("no outbound network connectivity")
    r = await registry.run(
        "http_get", {"op": "read", "url": "https://example.com/"}, ctx_for()
    )
    _assert_honest_read(r, "https://example.com/")
    if r.success:
        assert r.output["operation"] == "web_read"
        assert "Example Domain" in r.output["content"]
        assert r.output["title"].strip().lower().startswith("example")


@pytest.mark.network
async def test_real_web_read_wikipedia(registry):
    if not _reachable("en.wikipedia.org"):
        pytest.skip("no outbound network connectivity")
    url = "https://en.wikipedia.org/wiki/Hypertext_Transfer_Protocol"
    r = await registry.run(
        "http_get", {"op": "read", "url": url, "max_chars": 5000}, ctx_for()
    )
    _assert_honest_read(r, url)
    if r.success:
        assert len(r.output["content"]) > 100
        assert len(r.output["content"]) <= 5000, "max_chars must bound a live body"


@pytest.mark.network
async def test_real_web_search_returns_links(registry):
    if not _reachable("en.wikipedia.org"):
        pytest.skip("no outbound network connectivity")
    r = await registry.run(
        "http_get", {"op": "search", "query": "HTTP status codes", "max_results": 5}, ctx_for()
    )
    if not r.success:
        # The real API answered with an error status: that is the API's verdict,
        # not a regression here. The payload contract is pinned offline above.
        assert re.fullmatch(r"web_search failed: HTTP \d{3} from \S+", r.error or ""), (
            f"live search produced no HTTP verdict: {r.error!r}"
        )
        return
    assert r.output["operation"] == "web_search"
    assert r.output["engine"] == "wikipedia_api"
    assert r.output["result_count"] > 0
    first = r.output["results"][0]
    assert first["title"]
    assert first["url"].startswith("https://en.wikipedia.org/wiki/")
    assert "snippet" in first
