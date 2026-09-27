"""HTTP client tool for making web requests."""

import asyncio
from typing import Any, Optional

import httpx

from integrations.tools.base import BaseTool, ToolResult, ToolRiskLevel, ToolPermission


class HTTPClientTool(BaseTool):
    """Tool for making HTTP requests."""

    id = "http_client"
    name = "HTTP Client"
    description = "Make HTTP requests to external URLs"
    category = "network"
    risk_level = ToolRiskLevel.MEDIUM
    permissions = [ToolPermission.EXTERNAL_API]

    input_schema = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The URL to request",
            },
            "method": {
                "type": "string",
                "description": "HTTP method (GET, POST, PUT, DELETE, PATCH)",
                "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"],
                "default": "GET",
            },
            "headers": {
                "type": "object",
                "description": "Optional HTTP headers",
                "additionalProperties": {"type": "string"},
            },
            "body": {
                "type": "string",
                "description": "Request body (for POST, PUT, PATCH)",
            },
            "timeout": {
                "type": "integer",
                "description": "Request timeout in seconds (default: 30)",
                "default": 30,
            },
            "allow_redirects": {
                "type": "boolean",
                "description": "Whether to follow redirects (default: true)",
                "default": True,
            },
            "max_size": {
                "type": "integer",
                "description": "Maximum response size in bytes (default: 10MB)",
                "default": 10485760,
            },
        },
        "required": ["url"],
    }

    output_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Final URL after redirects"},
            "status_code": {"type": "integer", "description": "HTTP status code"},
            "headers": {"type": "object", "description": "Response headers"},
            "content": {"type": "string", "description": "Response body"},
            "content_type": {"type": "string", "description": "Content-Type of response"},
            "size": {"type": "integer", "description": "Response size in bytes"},
            "duration_ms": {"type": "integer", "description": "Request duration in milliseconds"},
        },
    }

    def __init__(
        self,
        allowed_domains: list[str] | None = None,
        allowed_schemes: list[str] | None = None,
    ):
        """Initialize the HTTP client tool.

        Args:
            allowed_domains: List of allowed domains. If None, all domains are allowed.
            allowed_schemes: List of allowed URL schemes. Default: https only.
        """
        super().__init__()
        self.allowed_domains = allowed_domains
        self.allowed_schemes = allowed_schemes or ["https"]

        # Create a shared client
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None:
            limits = httpx.Limits(max_connections=10, max_keepalive_connections=5)
            self._client = httpx.AsyncClient(limits=limits, timeout=30.0)
        return self._client

    async def execute(self, parameters: dict[str, Any]) -> ToolResult:
        """Execute the HTTP request."""
        import time

        start_time = time.time()

        try:
            url = parameters.get("url", "")
            method = parameters.get("method", "GET").upper()
            headers = parameters.get("headers", {})
            body = parameters.get("body")
            timeout = parameters.get("timeout", 30)
            allow_redirects = parameters.get("allow_redirects", True)
            max_size = parameters.get("max_size", 10485760)

            # Validate URL security
            validation_error = self._validate_url(url)
            if validation_error:
                return ToolResult(success=False, error=validation_error)

            # Get client
            client = self._get_client()

            # Prepare request
            request_headers = dict(headers)
            request_args = {
                "method": method,
                "url": url,
                "timeout": timeout,
                "follow_redirects": allow_redirects,
            }

            if headers:
                request_args["headers"] = request_headers

            if body and method in ("POST", "PUT", "PATCH"):
                request_args["content"] = body

            # Make request
            response = await client.request(**request_args)

            # Check response size
            content = response.text
            if len(content.encode('utf-8')) > max_size:
                return ToolResult(
                    success=False,
                    error=f"Response too large: {len(content)} bytes (max: {max_size} bytes)",
                )

            execution_time = int((time.time() - start_time) * 1000)

            # Build response
            response_headers = {k: v for k, v in response.headers.items()}

            return ToolResult(
                success=True,
                output={
                    "url": str(response.url),
                    "status_code": response.status_code,
                    "headers": response_headers,
                    "content": content,
                    "content_type": response.headers.get("content-type", ""),
                    "size": len(content.encode('utf-8')),
                    "duration_ms": execution_time,
                },
                execution_time_ms=execution_time,
            )

        except httpx.TimeoutException:
            execution_time = int((time.time() - start_time) * 1000)
            return ToolResult(
                success=False,
                error=f"Request timed out after {timeout} seconds",
                execution_time_ms=execution_time,
            )
        except httpx.HTTPStatusError as e:
            execution_time = int((time.time() - start_time) * 1000)
            return ToolResult(
                success=False,
                error=f"HTTP error: {e.response.status_code} {e.response.reason_phrase}",
                execution_time_ms=execution_time,
            )
        except httpx.RequestError as e:
            execution_time = int((time.time() - start_time) * 1000)
            return ToolResult(
                success=False,
                error=f"Request error: {str(e)}",
                execution_time_ms=execution_time,
            )
        except Exception as e:
            execution_time = int((time.time() - start_time) * 1000)
            return ToolResult(
                success=False,
                error=f"Error making request: {str(e)}",
                execution_time_ms=execution_time,
            )

    def _validate_url(self, url: str) -> Optional[str]:
        """Validate URL security."""
        from urllib.parse import urlparse

        try:
            parsed = urlparse(url)
        except Exception:
            return "Invalid URL format"

        # Check scheme
        if parsed.scheme.lower() not in self.allowed_schemes:
            return f"Scheme '{parsed.scheme}' not allowed. Allowed: {', '.join(self.allowed_schemes)}"

        # Check for localhost/127.0.0.1 to prevent SSRF
        hostname = parsed.hostname.lower() if parsed.hostname else ""
        if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            return "Access to localhost is not allowed"

        # Check private IP addresses
        if hostname.startswith(("10.", "172.16.", "192.168.", "169.254.")):
            return "Access to private network addresses is not allowed"

        # Check domain restrictions
        if self.allowed_domains:
            if hostname not in self.allowed_domains:
                return f"Domain '{hostname}' is not in allowed domains"

        return None

    async def close(self):
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
