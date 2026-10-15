from __future__ import annotations

import httpx
import pytest

from rag_mcp_server.backend_client import (
    BackendAuthError,
    BackendClient,
    BackendProtocolError,
    BackendUnavailableError,
)

LOGIN_URL = "http://backend.test/api/auth/login"
EXECUTE_URL = "http://backend.test/api/agent/tools/execute"


def _login_response(token: str = "jwt-1", expires_in: float = 86400) -> httpx.Response:
    return httpx.Response(
        200, json={"code": 0, "message": "ok", "data": {"token": token, "expiresIn": expires_in}}
    )


def _execute_response(
    name: str, success: bool = True, data: dict | None = None, failure_reason: str | None = None
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "code": 0,
            "message": "ok",
            "data": {
                "requestId": "11111111-1111-1111-1111-111111111111",
                "results": [
                    {
                        "name": name,
                        "success": success,
                        "data": data,
                        "failureReason": failure_reason,
                    }
                ],
            },
        },
    )


@pytest.mark.respx(base_url="http://backend.test")
async def test_logs_in_once_and_reuses_token(settings, respx_mock):
    login = respx_mock.post("/api/auth/login").mock(return_value=_login_response())
    execute = respx_mock.post("/api/agent/tools/execute").mock(
        return_value=_execute_response("search_knowledge", data={"found": True, "sources": []})
    )
    client = BackendClient(settings)
    try:
        await client.call_tool("search_knowledge", {"query": "x"})
        await client.call_tool("search_knowledge", {"query": "y"})
    finally:
        await client.aclose()
    assert login.call_count == 1
    assert execute.call_count == 2
    assert execute.calls[0].request.headers["authorization"] == "Bearer jwt-1"


@pytest.mark.respx(base_url="http://backend.test")
async def test_parses_a_successful_result(settings, respx_mock):
    respx_mock.post("/api/auth/login").mock(return_value=_login_response())
    respx_mock.post("/api/agent/tools/execute").mock(
        return_value=_execute_response("list_documents", data={"items": []})
    )
    client = BackendClient(settings)
    try:
        result = await client.call_tool("list_documents", {})
    finally:
        await client.aclose()
    assert result.success is True
    assert result.data == {"items": []}
    assert result.failure_reason is None


@pytest.mark.respx(base_url="http://backend.test")
async def test_parses_a_failed_result_without_raising(settings, respx_mock):
    respx_mock.post("/api/auth/login").mock(return_value=_login_response())
    respx_mock.post("/api/agent/tools/execute").mock(
        return_value=_execute_response(
            "get_document_status", success=False, failure_reason="PERMISSION_DENIED"
        )
    )
    client = BackendClient(settings)
    try:
        result = await client.call_tool("get_document_status", {"documentId": "x"})
    finally:
        await client.aclose()
    assert result.success is False
    assert result.failure_reason == "PERMISSION_DENIED"


@pytest.mark.respx(base_url="http://backend.test")
async def test_login_network_failure_raises_unavailable(settings, respx_mock):
    respx_mock.post("/api/auth/login").mock(side_effect=httpx.ConnectError("refused"))
    client = BackendClient(settings)
    try:
        with pytest.raises(BackendUnavailableError):
            await client.call_tool("search_knowledge", {"query": "x"})
    finally:
        await client.aclose()


@pytest.mark.respx(base_url="http://backend.test")
async def test_tool_call_network_failure_raises_unavailable(settings, respx_mock):
    respx_mock.post("/api/auth/login").mock(return_value=_login_response())
    respx_mock.post("/api/agent/tools/execute").mock(side_effect=httpx.ReadTimeout("slow"))
    client = BackendClient(settings)
    try:
        with pytest.raises(BackendUnavailableError):
            await client.call_tool("search_knowledge", {"query": "x"})
    finally:
        await client.aclose()


@pytest.mark.respx(base_url="http://backend.test")
async def test_login_rejected_raises_auth_error(settings, respx_mock):
    respx_mock.post("/api/auth/login").mock(
        return_value=httpx.Response(401, json={"code": 401, "message": "bad credentials"})
    )
    client = BackendClient(settings)
    try:
        with pytest.raises(BackendAuthError):
            await client.call_tool("search_knowledge", {"query": "x"})
    finally:
        await client.aclose()


@pytest.mark.respx(base_url="http://backend.test")
async def test_login_missing_fields_raises_protocol_error(settings, respx_mock):
    respx_mock.post("/api/auth/login").mock(
        return_value=httpx.Response(200, json={"code": 0, "data": {}})
    )
    client = BackendClient(settings)
    try:
        with pytest.raises(BackendProtocolError):
            await client.call_tool("search_knowledge", {"query": "x"})
    finally:
        await client.aclose()


@pytest.mark.respx(base_url="http://backend.test")
async def test_execute_non_200_raises_protocol_error(settings, respx_mock):
    respx_mock.post("/api/auth/login").mock(return_value=_login_response())
    respx_mock.post("/api/agent/tools/execute").mock(return_value=httpx.Response(500, text="boom"))
    client = BackendClient(settings)
    try:
        with pytest.raises(BackendProtocolError):
            await client.call_tool("search_knowledge", {"query": "x"})
    finally:
        await client.aclose()


@pytest.mark.respx(base_url="http://backend.test")
async def test_malformed_results_shape_raises_protocol_error(settings, respx_mock):
    respx_mock.post("/api/auth/login").mock(return_value=_login_response())
    respx_mock.post("/api/agent/tools/execute").mock(
        return_value=httpx.Response(
            200, json={"code": 0, "data": {"requestId": "x", "results": []}}
        )
    )
    client = BackendClient(settings)
    try:
        with pytest.raises(BackendProtocolError):
            await client.call_tool("search_knowledge", {"query": "x"})
    finally:
        await client.aclose()


@pytest.mark.respx(base_url="http://backend.test")
async def test_a_stale_cached_token_triggers_relogin_once_on_401(settings, respx_mock):
    login = respx_mock.post("/api/auth/login").mock(
        side_effect=[_login_response(token="jwt-1"), _login_response(token="jwt-2")]
    )
    execute = respx_mock.post("/api/agent/tools/execute").mock(
        side_effect=[
            httpx.Response(401, json={"code": 401, "message": "expired"}),
            _execute_response("search_knowledge", data={"found": False, "sources": []}),
        ]
    )
    client = BackendClient(settings)
    try:
        result = await client.call_tool("search_knowledge", {"query": "x"})
    finally:
        await client.aclose()
    assert result.success is True
    assert login.call_count == 2
    assert execute.call_count == 2
    assert execute.calls[1].request.headers["authorization"] == "Bearer jwt-2"


@pytest.mark.respx(base_url="http://backend.test")
async def test_persistent_401_raises_auth_error_after_one_retry(settings, respx_mock):
    respx_mock.post("/api/auth/login").mock(return_value=_login_response())
    respx_mock.post("/api/agent/tools/execute").mock(
        return_value=httpx.Response(401, json={"code": 401, "message": "nope"})
    )
    client = BackendClient(settings)
    try:
        with pytest.raises(BackendAuthError):
            await client.call_tool("search_knowledge", {"query": "x"})
    finally:
        await client.aclose()


@pytest.mark.respx(base_url="http://backend.test")
async def test_expired_cached_token_triggers_relogin_before_any_call(settings, respx_mock):
    login = respx_mock.post("/api/auth/login").mock(
        side_effect=[_login_response(token="jwt-1", expires_in=1), _login_response(token="jwt-2")]
    )
    execute = respx_mock.post("/api/agent/tools/execute").mock(
        return_value=_execute_response("search_knowledge", data={"found": False, "sources": []})
    )
    client = BackendClient(settings)
    try:
        await client.call_tool("search_knowledge", {"query": "x"})
        # token_refresh_margin_seconds=60 > expires_in=1, so this one is already "expired" locally.
        await client.call_tool("search_knowledge", {"query": "y"})
    finally:
        await client.aclose()
    assert login.call_count == 2
    assert execute.calls[1].request.headers["authorization"] == "Bearer jwt-2"
