"""Talks to the rag-knowledge-base Java backend: proxies one tool call to
`POST /api/agent/tools/execute` on behalf of one caller.

Identity: the caller's own bearer token is passed per call and used for exactly that call. It is
never stored on the client, so concurrent callers cannot see each other's tokens. A shared
service account is used only when the call passes ``token=None`` *and* the operator enabled the
degraded mode; otherwise ``token=None`` is refused here too (defense in depth).

Exception messages are fixed text plus an HTTP status code: never a response body, a header, or
a token, because exceptions end up in logs.
"""

from __future__ import annotations

import http.cookiejar
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings


class BackendUnavailableError(Exception):
    """The backend could not be reached at all (connection refused, DNS, timeout)."""


class BackendAuthError(Exception):
    """The backend rejected the credential (HTTP 401/403 on the tool endpoint or on login)."""

    def __init__(self, message: str, *, service_account: bool = False) -> None:
        super().__init__(message)
        self.service_account = service_account


class BackendProtocolError(Exception):
    """The backend responded, but not in the shape this client expects."""


@dataclass(frozen=True)
class ToolCallResult:
    name: str
    success: bool
    data: Any | None
    failure_reason: str | None


class _RejectAllCookies(http.cookiejar.CookiePolicy):
    """Never store or send cookies: a backend Set-Cookie on one caller's response must not be
    replayed on another caller's request through the shared connection pool."""

    netscape = True
    rfc2965 = False
    hide_cookie2 = False

    def set_ok(self, cookie: Any, request: Any) -> bool:
        return False

    def return_ok(self, cookie: Any, request: Any) -> bool:
        return False

    def domain_return_ok(self, domain: str, request: Any) -> bool:
        return False

    def path_return_ok(self, path: str, request: Any) -> bool:
        return False


class BackendClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._base_url = settings.backend_base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=settings.request_timeout_seconds)
        self._client.cookies.jar.set_policy(_RejectAllCookies())
        # Service-account state; untouched unless the degraded mode is enabled and used.
        self._sa_token: str | None = None
        self._sa_expires_at: float = 0.0

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, token: str | None
    ) -> ToolCallResult:
        if token is not None:
            response = await self._post_tool_call(token, name, arguments)
            if response.status_code in (401, 403):
                # No retry, no re-login, no fallback: the caller's credential is the only one
                # that may ever be used on their behalf.
                raise BackendAuthError(
                    f"Backend rejected the caller's token (HTTP {response.status_code})."
                )
        else:
            response = await self._call_as_service_account(name, arguments)
        if response.status_code != 200:
            raise BackendProtocolError(
                f"Backend returned HTTP {response.status_code} for a tool call."
            )
        return _parse_tool_call_response(name, response)

    # --- degraded mode: shared service account ------------------------------------------

    async def _call_as_service_account(
        self, name: str, arguments: dict[str, Any]
    ) -> httpx.Response:
        if not self._settings.allow_shared_service_account:
            raise BackendAuthError("No caller token and the shared service account is disabled.")
        token = await self._sa_ensure_token()
        response = await self._post_tool_call(token, name, arguments)
        if response.status_code in (401, 403):
            token = await self._sa_login()  # stale cache: one fresh login, then give up
            response = await self._post_tool_call(token, name, arguments)
            if response.status_code in (401, 403):
                raise BackendAuthError(
                    "Backend rejected the shared service account's credentials.",
                    service_account=True,
                )
        return response

    async def _sa_ensure_token(self) -> str:
        if self._sa_token is None or time.time() >= self._sa_expires_at:
            return await self._sa_login()
        return self._sa_token

    async def _sa_login(self) -> str:
        assert self._settings.service_username and self._settings.service_password is not None
        try:
            response = await self._client.post(
                f"{self._base_url}/api/auth/login",
                json={
                    "username": self._settings.service_username,
                    "password": self._settings.service_password.get_secret_value(),
                },
            )
        except httpx.HTTPError as exc:
            raise BackendUnavailableError(
                f"Could not reach the backend to log in ({type(exc).__name__})."
            ) from None
        if response.status_code != 200:
            raise BackendAuthError(
                f"Service-account login failed (HTTP {response.status_code}).",
                service_account=True,
            )
        body = _safe_json(response)
        data = body.get("data") if isinstance(body, dict) else None
        token = data.get("token") if isinstance(data, dict) else None
        expires_in = data.get("expiresIn") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token or not isinstance(expires_in, (int, float)):
            raise BackendProtocolError("Login response did not contain the expected fields.")
        self._sa_token = token
        self._sa_expires_at = time.time() + expires_in - self._settings.token_refresh_margin_seconds
        return token

    # --- shared plumbing -----------------------------------------------------------------

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
            # `from None`: the chained request object carries the Authorization header.
            raise BackendUnavailableError(
                f"Could not reach the backend for a tool call ({type(exc).__name__})."
            ) from None


def _parse_tool_call_response(name: str, response: httpx.Response) -> ToolCallResult:
    body = _safe_json(response)
    data = body.get("data") if isinstance(body, dict) else None
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list) or len(results) != 1:
        raise BackendProtocolError("Expected exactly one result for the tool call.")
    result = results[0]
    if not isinstance(result, dict) or "success" not in result:
        raise BackendProtocolError("Malformed tool result.")
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
