from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from rag_mcp_server.backend_client import (
    BackendAuthError,
    BackendClient,
    BackendProtocolError,
    BackendUnavailableError,
)
from rag_mcp_server.config import Settings

from .conftest import TOKEN_A, TOKEN_B, execute_ok


async def _call(client: BackendClient, token: str | None = TOKEN_A, **kw):
    try:
        return await client.call_tool(
            "search_knowledge", kw.get("args", {"query": "x"}), token=token
        )
    finally:
        await client.aclose()


@pytest.mark.respx(base_url="http://backend.test", assert_all_called=False)
async def test_forwards_exactly_the_callers_token(settings, respx_mock):
    execute = respx_mock.post("/api/agent/tools/execute").mock(
        return_value=execute_ok("search_knowledge", {"found": True})
    )
    result = await _call(BackendClient(settings))
    assert result.success is True and result.data == {"found": True}
    assert execute.calls[0].request.headers["authorization"] == f"Bearer {TOKEN_A}"


@pytest.mark.respx(base_url="http://backend.test", assert_all_called=False)
async def test_two_callers_on_one_client_never_cross(settings, respx_mock):
    execute = respx_mock.post("/api/agent/tools/execute").mock(
        return_value=execute_ok("search_knowledge")
    )
    client = BackendClient(settings)
    try:
        await asyncio.gather(
            client.call_tool("search_knowledge", {"query": "a"}, token=TOKEN_A),
            client.call_tool("search_knowledge", {"query": "b"}, token=TOKEN_B),
        )
        await client.call_tool("search_knowledge", {"query": "c"}, token=TOKEN_A)
    finally:
        await client.aclose()
    by_query = {
        json.loads(c.request.content)["calls"][0]["arguments"]["query"]: c.request
        for c in execute.calls
    }
    assert by_query["a"].headers["authorization"] == f"Bearer {TOKEN_A}"
    assert by_query["b"].headers["authorization"] == f"Bearer {TOKEN_B}"
    assert by_query["c"].headers["authorization"] == f"Bearer {TOKEN_A}"


@pytest.mark.respx(base_url="http://backend.test", assert_all_called=False)
@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_caller_token_raises_without_retry_or_login(settings, respx_mock, status):
    login = respx_mock.post("/api/auth/login").mock(return_value=httpx.Response(200))
    execute = respx_mock.post("/api/agent/tools/execute").mock(
        return_value=httpx.Response(status, json={"message": "expired"})
    )
    with pytest.raises(BackendAuthError) as exc:
        await _call(BackendClient(settings))
    assert exc.value.service_account is False
    assert execute.call_count == 1 and login.call_count == 0


@pytest.mark.respx(base_url="http://backend.test", assert_all_called=False)
async def test_token_none_is_refused_when_degraded_mode_is_off(settings, respx_mock):
    login = respx_mock.post("/api/auth/login")
    execute = respx_mock.post("/api/agent/tools/execute")
    with pytest.raises(BackendAuthError):
        await _call(BackendClient(settings), token=None)
    assert login.call_count == 0 and execute.call_count == 0


@pytest.mark.respx(base_url="http://backend.test", assert_all_called=False)
async def test_backend_cookies_are_never_replayed_across_callers(settings, respx_mock):
    execute = respx_mock.post("/api/agent/tools/execute").mock(
        side_effect=[
            httpx.Response(
                200,
                headers={"set-cookie": "JSESSIONID=victim; Path=/"},
                json={"data": {"results": [{"name": "search_knowledge", "success": True}]}},
            ),
            execute_ok("search_knowledge"),
        ]
    )
    client = BackendClient(settings)
    try:
        await client.call_tool("search_knowledge", {"query": "a"}, token=TOKEN_A)
        await client.call_tool("search_knowledge", {"query": "b"}, token=TOKEN_B)
    finally:
        await client.aclose()
    assert "cookie" not in execute.calls[1].request.headers


@pytest.mark.respx(base_url="http://backend.test", assert_all_called=False)
async def test_network_failure_raises_unavailable_without_chaining(settings, respx_mock):
    respx_mock.post("/api/agent/tools/execute").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(BackendUnavailableError) as exc:
        await _call(BackendClient(settings))
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


@pytest.mark.respx(base_url="http://backend.test", assert_all_called=False)
async def test_non_200_error_omits_the_response_body(settings, respx_mock):
    respx_mock.post("/api/agent/tools/execute").mock(
        return_value=httpx.Response(500, text="SECRET-DOC-BODY")
    )
    with pytest.raises(BackendProtocolError) as exc:
        await _call(BackendClient(settings))
    assert "SECRET-DOC-BODY" not in str(exc.value) and "500" in str(exc.value)


@pytest.mark.respx(base_url="http://backend.test", assert_all_called=False)
async def test_malformed_results_shape_raises_protocol_error(settings, respx_mock):
    respx_mock.post("/api/agent/tools/execute").mock(
        return_value=httpx.Response(200, json={"data": {"results": []}})
    )
    with pytest.raises(BackendProtocolError):
        await _call(BackendClient(settings))


@pytest.mark.respx(base_url="http://backend.test", assert_all_called=False)
async def test_failed_result_is_parsed_without_raising(settings, respx_mock):
    respx_mock.post("/api/agent/tools/execute").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "results": [
                        {"name": "x", "success": False, "failureReason": "PERMISSION_DENIED"}
                    ]
                }
            },
        )
    )
    result = await _call(BackendClient(settings))
    assert result.success is False and result.failure_reason == "PERMISSION_DENIED"


# --- degraded mode: explicit shared service account -------------------------------------


def _sa_settings() -> Settings:
    return Settings(
        backend_base_url="http://backend.test",
        allow_shared_service_account=True,
        service_username="svc-mcp",
        service_password="not-a-real-password",
    )


def _login(token: str) -> httpx.Response:
    return httpx.Response(200, json={"data": {"token": token, "expiresIn": 86400}})


@pytest.mark.respx(base_url="http://backend.test", assert_all_called=False)
async def test_service_account_logs_in_once_and_reuses_token(respx_mock):
    login = respx_mock.post("/api/auth/login").mock(return_value=_login("sa-jwt"))
    execute = respx_mock.post("/api/agent/tools/execute").mock(
        return_value=execute_ok("search_knowledge")
    )
    client = BackendClient(_sa_settings())
    try:
        await client.call_tool("search_knowledge", {"query": "x"}, token=None)
        await client.call_tool("search_knowledge", {"query": "y"}, token=None)
    finally:
        await client.aclose()
    assert login.call_count == 1
    assert execute.calls[1].request.headers["authorization"] == "Bearer sa-jwt"


@pytest.mark.respx(base_url="http://backend.test", assert_all_called=False)
async def test_service_account_relogins_once_on_stale_token(respx_mock):
    login = respx_mock.post("/api/auth/login").mock(side_effect=[_login("sa-1"), _login("sa-2")])
    execute = respx_mock.post("/api/agent/tools/execute").mock(
        side_effect=[httpx.Response(401), execute_ok("search_knowledge")]
    )
    result = await _call(BackendClient(_sa_settings()), token=None)
    assert result.success and login.call_count == 2
    assert execute.calls[1].request.headers["authorization"] == "Bearer sa-2"


@pytest.mark.respx(base_url="http://backend.test", assert_all_called=False)
async def test_service_account_login_rejected_is_flagged_as_service_account(respx_mock):
    respx_mock.post("/api/auth/login").mock(return_value=httpx.Response(401, text="BODY"))
    with pytest.raises(BackendAuthError) as exc:
        await _call(BackendClient(_sa_settings()), token=None)
    assert exc.value.service_account is True and "BODY" not in str(exc.value)
