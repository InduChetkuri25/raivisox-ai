"""Real HTTP GET adapter — safe outbound reads with a hard domain allowlist.

What is REAL here:
- `web_read` performs a real HTTP GET to an allowlisted host via httpx and
  returns the actual response (status, headers, text body, title).
- `web_search` performs a real search query against Wikipedia's public
  search API (en.wikipedia.org/w/api.php) and returns real result
  titles, links, and snippets from the response.

Security boundaries (all enforced before any byte is sent):
- GET only. No other method is implemented or accepted.
- Scheme must be https (http:// is rejected outright).
- The URL host must match the domain allowlist exactly (no wildcards,
  no subdomain suffix tricks — the full host must be a list member).
- Port must be the scheme default; userinfo (`user@host`) is rejected.
- IP-literal hosts are rejected (addresses are not allowlisted domains).
- DNS is resolved before connecting: if ANY resolved address is
  loopback / private / link-local / multicast / non-global, the request
  is refused (SSRF guard).
- Redirects are NEVER followed automatically: each hop's Location is
  re-validated (allowlist + SSRF) and re-fetched explicitly, max 3 hops.
- Response size is capped; only text-like content types are accepted.
- Every privileged run requires Permission.NETWORK_REQUEST via the
  SecurityKernel gate in AdapterRegistry.
"""

import ipaddress
import json
import os
import re
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from integrations.adapters.base import AdapterContext, AdapterResult, ToolAdapter
from services.graph import events as graph_events
from services.graph import schema as graph_schema
from services.graph.provenance import ProvenanceError

MAX_RESPONSE_BYTES = 2_000_000  # 2 MB
MAX_REDIRECT_HOPS = 3
DEFAULT_TIMEOUT_SECONDS = 15.0

# Hosts a research step may talk to. Extend via RAIVISOX_HTTP_ALLOWED_HOSTS
# (comma-separated) — evaluated at construction time, not per request.
DEFAULT_ALLOWED_HOSTS = [
    "example.com",
    "www.example.com",
    "en.wikipedia.org",
    "www.wikipedia.org",
    "html.duckduckgo.com",
    "duckduckgo.com",
    "www.duckduckgo.com",
]

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_BUILTIN_USER_AGENT = (
    "RAIVISOX-HttpGetAdapter/1.0 (autonomous research pipeline; "
    "allowlist-enforced GET-only; contact: ops@raivisox.local)"
)


def _strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub("", html)).strip()


def _host_allowed(host: str, allowlist: set[str]) -> bool:
    return host.lower() in allowlist


# --- V3 Phase 1 endpoint events (host + path only; never query/headers) ----

def _linkage_kwargs(ctx: AdapterContext) -> dict:
    return {
        "user_id": getattr(ctx, "user_id", "") or None,
        "task_id": getattr(ctx, "task_id", "") or None,
        "run_id": getattr(ctx, "execution_run_id", "") or None,
        "step_node_id": getattr(ctx, "step_id", "") or None,
        "action_id": getattr(ctx, "action_id", "") or None,
    }


def _endpoint_parts(url: str) -> tuple[str | None, str | None]:
    """Host and path template only — query strings can carry tokens."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None, None
    host = (parts.hostname or "").lower() or None
    path = parts.path or "/"
    return host, path


def _denial_reason(message: str | None) -> str:
    text = (message or "").lower()
    if "only https" in text:
        return "scheme_not_https"
    if "no hostname" in text:
        return "missing_host"
    if "userinfo" in text:
        return "userinfo_present"
    if "custom ports" in text:
        return "port_not_default"
    if "allowlist" in text:
        return "host_not_allowlisted"
    if "ip-literal" in text:
        return "ip_literal"
    if "non-public" in text or "ssrf" in text or "dns" in text:
        return "non_public_address"
    if "redirect" in text:
        return "redirect_refused"
    return "unknown"


async def _record_endpoint(
    ctx: AdapterContext,
    *,
    event_type: str,
    outcome: str,
    host: str | None,
    path: str | None,
    payload: dict,
) -> None:
    """Emit an endpoint event; recording never changes the outcome."""
    if not getattr(ctx, "action_id", ""):
        return
    target = graph_schema.endpoint_target(host, path) if host and path else None
    try:
        await graph_events.emit(
            event_type=event_type,
            outcome=outcome,
            actor_id=ctx.actor_id,
            actor_type="service",
            user_id=getattr(ctx, "user_id", "") or "",
            task_id=getattr(ctx, "task_id", "") or "",
            run_id=getattr(ctx, "execution_run_id", "") or None,
            step_node_id=getattr(ctx, "step_id", "") or None,
            tool_id="http_get",
            action_id=ctx.action_id,
            target_kind="endpoint" if target else None,
            target_id=target,
            payload=payload,
        )
    except ProvenanceError:
        raise
    except Exception:  # noqa: BLE001 - observation must not fail the action
        return


def _all_ips_global(host: str) -> tuple[bool, str]:
    """Resolve the host and refuse it if any address is non-global (SSRF guard)."""
    import socket

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        return False, f"DNS resolution failed for '{host}': {exc}"
    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw.split("%")[0])
        except ValueError:
            return False, f"Unparseable resolved address '{raw}' for '{host}'"
        v4 = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) else None
        if v4 is not None:
            ip = v4
        if not ip.is_global or ip.is_multicast:
            return False, (
                f"Refusing non-public resolved address {ip} for host '{host}' (SSRF guard)"
            )
    return True, ""


class HttpGetAdapter(ToolAdapter):
    """Real outbound HTTP GET restricted to an explicit domain allowlist."""

    id = "http_get"
    description = (
        "Real HTTP GET to allowlisted hosts (web_read) and real search via "
        "Wikipedia's public API (web_search). GET-only, redirects "
        "re-validated per hop, SSRF-guarded, size-capped."
    )
    kernel_permissions = ["network_request"]

    def __init__(self, allowed_hosts: list[str] | None = None):
        env_hosts = [
            h.strip().lower()
            for h in os.environ.get("RAIVISOX_HTTP_ALLOWED_HOSTS", "").split(",")
            if h.strip()
        ]
        self.allowed_hosts: set[str] = set(
            (allowed_hosts or DEFAULT_ALLOWED_HOSTS) + env_hosts
        )

    # -- URL validation ----------------------------------------------------

    def _check_url(self, url: str) -> tuple[bool, str]:
        parts = urlsplit(url)
        if parts.scheme != "https":
            return False, f"Only https:// URLs are allowed (got '{parts.scheme or 'none'}://')"
        if not parts.hostname:
            return False, "URL has no hostname"
        if parts.username or parts.password:
            return False, "URLs with embedded userinfo are not allowed"
        if parts.port is not None:
            return False, f"Custom ports are not allowed (got {parts.port})"
        host = parts.hostname.lower()
        if not _host_allowed(host, self.allowed_hosts):
            return False, (
                f"Host '{host}' is not in the domain allowlist "
                f"(allowed: {sorted(self.allowed_hosts)})"
            )
        try:
            ipaddress.ip_address(host)
            return False, "IP-literal hosts are not allowed; use an allowlisted domain"
        except ValueError:
            pass
        ok, reason = _all_ips_global(host)
        if not ok:
            return False, reason
        return True, ""

    # -- fetching -----------------------------------------------------------

    async def _fetch(
        self, url: str, headers: dict[str, str]
    ) -> tuple[bool, str | None, int, str, dict[str, str]]:
        """GET a validated URL, following (and re-validating) up to 3 redirects."""
        current = url
        for _hop in range(MAX_REDIRECT_HOPS + 1):
            ok, reason = self._check_url(current)
            if not ok:
                return False, reason, 0, "", {}
            async with httpx.AsyncClient(
                follow_redirects=False, timeout=DEFAULT_TIMEOUT_SECONDS
            ) as client:
                try:
                    async with client.stream("GET", current, headers=headers) as resp:
                        if resp.is_redirect or 100 <= resp.status_code < 200:
                            location = resp.headers.get("location", "")
                            if not location:
                                return False, (
                                    f"Redirect without Location header (status {resp.status_code})"
                                ), resp.status_code, "", {}
                            current = urljoin(current, location)
                            continue
                        if resp.status_code != 200:
                            return (
                                False,
                                f"HTTP {resp.status_code} from {current}",
                                resp.status_code,
                                "",
                                dict(resp.headers),
                            )
                        ctype = resp.headers.get("content-type", "")
                        if not ctype.lower().startswith(
                            ("text/", "application/json", "application/xml", "application/xhtml")
                        ):
                            return (
                                False,
                                f"Refusing non-text content type '{ctype}'",
                                resp.status_code,
                                "",
                                {},
                            )
                        chunks: list[bytes] = []
                        received = 0
                        too_big = False
                        async for chunk in resp.aiter_bytes(64 * 1024):
                            received += len(chunk)
                            if received > MAX_RESPONSE_BYTES:
                                too_big = True
                                break
                            chunks.append(chunk)
                        if too_big:
                            return (
                                False,
                                f"Response exceeds {MAX_RESPONSE_BYTES} byte cap",
                                resp.status_code,
                                "",
                                {},
                            )
                        text = b"".join(chunks).decode(resp.encoding or "utf-8", "replace")
                        return True, None, resp.status_code, text, dict(resp.headers)
                except httpx.HTTPError as exc:
                    return False, f"HTTP request failed: {exc}", 0, "", {}
        return False, f"More than {MAX_REDIRECT_HOPS} redirects", 0, "", {}

    # -- operations -----------------------------------------------------------

    async def run(self, params: dict[str, Any], ctx: AdapterContext) -> AdapterResult:
        import time

        started = time.perf_counter()

        def _done(success: bool, output=None, error: str | None = None) -> AdapterResult:
            return AdapterResult(
                success=success,
                tool_id=self.id,
                output=output,
                error=error,
                execution_time_ms=int((time.perf_counter() - started) * 1000),
            )

        op = (params.get("op") or "read").lower()
        headers = {"User-Agent": params.get("user_agent") or _BUILTIN_USER_AGENT}

        if op == "read":
            url = params.get("url")
            if not url or not isinstance(url, str):
                return _done(False, error="web_read requires a 'url' string parameter")
            host, path_template = _endpoint_parts(url)
            allowed, reason = self._check_url(url)
            if not allowed:
                # Refused before any byte: a denial, never an attempt.
                await _record_endpoint(
                    ctx,
                    event_type=graph_schema.ENDPOINT_DENIED,
                    outcome=graph_schema.DENIED,
                    host=host,
                    path=path_template,
                    payload={
                        **({"host": host} if host else {}),
                        **({"path_template": path_template} if path_template else {}),
                        "reason": _denial_reason(reason),
                        "denial_layer": "adapter_policy",
                    },
                )
                return _done(False, error=reason)
            await _record_endpoint(
                ctx,
                event_type=graph_schema.ENDPOINT_ATTEMPTED,
                outcome=graph_schema.ATTEMPTED,
                host=host,
                path=path_template,
                payload={
                    **({"host": host} if host else {}),
                    **({"path_template": path_template} if path_template else {}),
                },
            )
            ok, error, status, text, hdrs = await self._fetch(url, headers)
            if not ok:
                # Attempted but no verdict we can honestly record: the
                # absence of a terminal event means "unknown" (spec §5.10).
                return _done(False, error=error)
            await _record_endpoint(
                ctx,
                event_type=graph_schema.ENDPOINT_SUCCEEDED,
                outcome=graph_schema.EXECUTED,
                host=host,
                path=path_template,
                payload={
                    **({"host": host} if host else {}),
                    **({"path_template": path_template} if path_template else {}),
                    "status": status,
                    "bytes": len(text),
                },
            )
            title_match = _TITLE_RE.search(text)
            return _done(
                True,
                output={
                    "operation": "web_read",
                    "url": url,
                    "status": status,
                    "content_type": hdrs.get("content-type", ""),
                    "title": _strip_tags(title_match.group(1)) if title_match else "",
                    "bytes": len(text),
                    "content": text[: params.get("max_chars", 20_000)],
                },
            )

        if op == "search":
            query = params.get("query")
            if not query or not isinstance(query, str):
                return _done(False, error="web_search requires a 'query' string parameter")
            limit = max(1, min(int(params.get("max_results", 10)), 20))
            api_url = (
                "https://en.wikipedia.org/w/api.php?action=query&list=search"
                f"&srsearch={quote(query, safe='')}"
                f"&format=json&srlimit={limit}"
            )
            api_host, api_path = _endpoint_parts(api_url)
            api_allowed, api_reason = self._check_url(api_url)
            if not api_allowed:
                await _record_endpoint(
                    ctx,
                    event_type=graph_schema.ENDPOINT_DENIED,
                    outcome=graph_schema.DENIED,
                    host=api_host,
                    path=api_path,
                    payload={
                        **({"host": api_host} if api_host else {}),
                        **({"path_template": api_path} if api_path else {}),
                        "reason": _denial_reason(api_reason),
                        "denial_layer": "adapter_policy",
                    },
                )
                return _done(False, error=f"web_search failed: {api_reason}")
            await _record_endpoint(
                ctx,
                event_type=graph_schema.ENDPOINT_ATTEMPTED,
                outcome=graph_schema.ATTEMPTED,
                host=api_host,
                path=api_path,
                payload={
                    **({"host": api_host} if api_host else {}),
                    **({"path_template": api_path} if api_path else {}),
                },
            )
            ok, error, status, text, _hdrs = await self._fetch(api_url, headers)
            if not ok:
                return _done(False, error=f"web_search failed: {error}")
            await _record_endpoint(
                ctx,
                event_type=graph_schema.ENDPOINT_SUCCEEDED,
                outcome=graph_schema.EXECUTED,
                host=api_host,
                path=api_path,
                payload={
                    **({"host": api_host} if api_host else {}),
                    **({"path_template": api_path} if api_path else {}),
                    "status": status,
                    "bytes": len(text),
                },
            )
            try:
                data = json.loads(text)
            except ValueError:
                return _done(False, error="web_search: invalid JSON from search API")
            results = []
            for item in data.get("query", {}).get("search", []):
                title = item.get("title", "")
                results.append(
                    {
                        "title": title,
                        "url": "https://en.wikipedia.org/wiki/"
                        + quote(title.replace(" ", "_"), safe=""),
                        "snippet": _strip_tags(item.get("snippet", "")),
                    }
                )
            return _done(
                True,
                output={
                    "operation": "web_search",
                    "query": query,
                    "engine": "wikipedia_api",
                    "result_count": len(results),
                    "total_hits": data.get("query", {}).get("searchinfo", {}).get("totalhits"),
                    "results": results,
                },
            )

        return _done(False, error=f"Unknown op '{op}' (expected 'read' or 'search')")
