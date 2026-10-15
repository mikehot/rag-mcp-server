"""Talks to the rag-knowledge-base Java backend: logs in as the configured service
account, caches and refreshes the JWT, and proxies a single tool call to
`POST /api/agent/tools/execute`.

Deliberately calls that plain REST endpoint rather than the backend's own `/mcp`
JSON-RPC adapter (see README "Design notes"): this process is already the real MCP
layer, so re-wrapping JSON-RPC inside JSON-RPC would add framing with no benefit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings


class BackendUnavailableError(Exception):
    """The backend could not be reached at all (connection refused, DNS, timeout)."""


class BackendAuthError(Exception):
    """Login failed, or a call was rejected as unauthenticated even after a fresh login."""


class BackendProtocolError(Exception):
    """The backend responded, but not in the shape this client expects."""


@dataclass(frozen=True)
class ToolCallResult:
    name: str
    success: bool
    data: Any | None
    failure_reason: str | None


class BackendClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._base_url = settings.backend_base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=settings.request_timeout_seconds)
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolCallResult:
        token = await self._ensure_token()
        response = await self._post_tool_call(token, name, arguments)
        if response.status_code == 401:
            # The cached token looked valid locally but the backend rejected it (revoked
            # account, clock skew, server restart). Log in exactly once more before giving up.
            token = await self._login()
            response = await self._post_tool_call(token, name, arguments)
            if response.status_code == 401:
                raise BackendAuthError(
                    "Backend rejected the service account's credentials even after a fresh "
                    "login; check RAG_MCP_SERVICE_USERNAME/RAG_MCP_SERVICE_PASSWORD and that "
                    "the account is active."
                )
        if response.status_code != 200:
            raise BackendProtocolError(
                f"Backend returned HTTP {response.status_code} for tool {name!r}: "
                f"{_safe_body(response)}"
            )
        return _parse_tool_call_response(name, response)

    async def _ensure_token(self) -> str:
        if self._token is None or time.time() >= self._token_expires_at:
            return await self._login()
        return self._token

    async def _login(self) -> str:
        try:
            response = await self._client.post(
                f"{self._base_url}/api/auth/login",
                json={
                    "username": self._settings.service_username,
                    "password": self._settings.service_password.get_secret_value(),
                },
            )
        except httpx.HTTPError as exc:
            raise BackendUnavailableError(f"Could not reach the backend to log in: {exc}") from exc
        if response.status_code != 200:
            raise BackendAuthError(
                f"Login failed with HTTP {response.status_code}: {_safe_body(response)}"
            )
        body = _safe_json(response)
        data = body.get("data") if isinstance(body, dict) else None
        token = data.get("token") if isinstance(data, dict) else None
        expires_in = data.get("expiresIn") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token or not isinstance(expires_in, (int, float)):
            raise BackendProtocolError(
                "Login response did not contain the expected token/expiresIn fields"
            )
        self._token = token
        self._token_expires_at = (
            time.time() + expires_in - self._settings.token_refresh_margin_seconds
        )
        return token

    async def _post_tool_call(
        self, token: str, name: str, arguments: dict[str, Any]
    ) -> httpx.Response:
        try:
            return await self._client.post(
                f"{self._base_url}/api/agent/tools/execute",
                headers={"Authorization": f"Bearer {token}"},
                json={"calls": [{"name": name, "arguments": arguments}]},
            )
        except httpx.HTTPError as exc:
            raise BackendUnavailableError(
                f"Could not reach the backend for tool {name!r}: {exc}"
            ) from exc


def _parse_tool_call_response(name: str, response: httpx.Response) -> ToolCallResult:
    body = _safe_json(response)
    data = body.get("data") if isinstance(body, dict) else None
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list) or len(results) != 1:
        raise BackendProtocolError(
            f"Expected exactly one result for tool {name!r}, got: {results!r}"
        )
    result = results[0]
    if not isinstance(result, dict) or "success" not in result:
        raise BackendProtocolError(f"Malformed tool result for {name!r}: {result!r}")
    return ToolCallResult(
        name=result.get("name", name),
        success=bool(result["success"]),
        data=result.get("data"),
        failure_reason=result.get("failureReason"),
    )


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _safe_body(response: httpx.Response) -> str:
    # Truncated: this can end up in a log line or a ToolError message, and the backend's
    # own error bodies are short JSON, not secrets -- but never echo request headers here.
    return response.text[:300]
