"""Per-caller identity: what the backend sees, and that nothing falls back to a shared account."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from rag_mcp_server.config import Settings
from rag_mcp_server.server import create_server

from .conftest import TOKEN_A, TOKEN_B, execute_ok, http_mode_client, mcp_call

BACKEND = "http://backend.test"
SA_LOGIN_PASSWORD = "svc-password-do-not-use"


def _auth_headers(route) -> list[str | None]:
    return [c.request.headers.get("authorization") for c in route.calls]


def _query(call) -> str:
    return json.loads(call.request.content)["calls"][0]["arguments"]["query"]


# --- stdio ---------------------------------------------------------------------------------


@pytest.mark.respx(base_url=BACKEND, assert_all_called=False)
async def test_stdio_users_a_and_b_each_reach_the_backend_as_themselves(respx_mock):
    execute = respx_mock.post("/api/agent/tools/execute").mock(
        return_value=execute_ok("search_knowledge")
    )
    for token in (TOKEN_A, TOKEN_B):  # two separate user processes
        server = create_server(Settings(backend_base_url=BACKEND, user_token=token))
        await server.call_tool("search_knowledge", {"query": "q"})
    assert _auth_headers(execute) == [f"Bearer {TOKEN_A}", f"Bearer {TOKEN_B}"]


@pytest.mark.respx(base_url=BACKEND, assert_all_called=False)
@pytest.mark.parametrize(
    ("user_token", "expect"),
    [
        (None, "RAG_USER_TOKEN"),
        ("   ", "RAG_USER_TOKEN"),
        ("Bearer abc.def", "malformed"),
        ("abc def", "malformed"),
        ("abc\r\nX-Evil: 1", "malformed"),
        ('"quoted"', "malformed"),
        ("a" * 9000, "malformed"),
    ],
    ids=["none", "blank", "bearer-prefix", "space", "crlf", "quotes", "too-long"],
)
async def test_stdio_missing_or_malformed_token_is_refused_with_no_backend_request(
    respx_mock, user_token, expect
):
    login = respx_mock.post("/api/auth/login")
    execute = respx_mock.post("/api/agent/tools/execute")
    server = create_server(Settings(backend_base_url=BACKEND, user_token=user_token))
    with pytest.raises(ToolError, match=expect):
        await server.call_tool("search_knowledge", {"query": "q"})
    assert execute.call_count == 0 and login.call_count == 0


@pytest.mark.respx(base_url=BACKEND, assert_all_called=False)
@pytest.mark.parametrize("status", [401, 403])
async def test_stdio_expired_or_rejected_token_errors_and_sends_nothing_else(
    respx_mock, settings, status
):
    login = respx_mock.post("/api/auth/login")
    execute = respx_mock.post("/api/agent/tools/execute").mock(
        return_value=httpx.Response(status, json={"message": "expired"})
    )
    server = create_server(settings)
    with pytest.raises(ToolError, match="expired, revoked, or invalid"):
        await server.call_tool("search_knowledge", {"query": "q"})
    assert execute.call_count == 1 and login.call_count == 0
    assert _auth_headers(execute) == [f"Bearer {TOKEN_A}"]


@pytest.mark.respx(base_url=BACKEND, assert_all_called=False)
async def test_stdio_token_is_stripped_of_surrounding_whitespace(respx_mock):
    execute = respx_mock.post("/api/agent/tools/execute").mock(
        return_value=execute_ok("search_knowledge")
    )
    server = create_server(Settings(backend_base_url=BACKEND, user_token=f"{TOKEN_A}\n"))
    await server.call_tool("search_knowledge", {"query": "q"})
    assert _auth_headers(execute) == [f"Bearer {TOKEN_A}"]


# --- streamable HTTP -----------------------------------------------------------------------


@pytest.mark.respx(base_url=BACKEND, assert_all_called=False)
async def test_http_header_token_is_passed_through_per_request(respx_mock):
    execute = respx_mock.post("/api/agent/tools/execute").mock(
        return_value=execute_ok("search_knowledge")
    )
    async with http_mode_client() as (client, _):
        await mcp_call(client, "search_knowledge", {"query": "a"}, TOKEN_A)
        await mcp_call(client, "search_knowledge", {"query": "b"}, TOKEN_B)
        await mcp_call(client, "search_knowledge", {"query": "c"}, TOKEN_A)
    assert _auth_headers(execute) == [f"Bearer {t}" for t in (TOKEN_A, TOKEN_B, TOKEN_A)]


@pytest.mark.respx(base_url=BACKEND, assert_all_called=False)
async def test_http_concurrent_callers_never_cross_identities(respx_mock):
    async def slow_backend(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01 if request.headers["authorization"].endswith("aaa") else 0.0)
        return execute_ok("search_knowledge")

    execute = respx_mock.post("/api/agent/tools/execute").mock(side_effect=slow_backend)
    n = 20
    async with http_mode_client() as (client, _):
        jobs = [
            mcp_call(client, "search_knowledge", {"query": f"{who}-{i}"}, tok, req_id=i)
            for i in range(n)
            for who, tok in (("A", TOKEN_A), ("B", TOKEN_B))
        ]
        results = await asyncio.gather(*jobs)
    assert not any(r.get("isError") for r in results)
    assert execute.call_count == 2 * n
    for call in execute.calls:
        expected = TOKEN_A if _query(call).startswith("A-") else TOKEN_B
        assert call.request.headers["authorization"] == f"Bearer {expected}"


@pytest.mark.respx(base_url=BACKEND, assert_all_called=False)
@pytest.mark.parametrize(
    "header",
    [None, "Basic dXNlcjpwdw==", "Bearer", "Bearer ", "Bearer a b", "Token abc"],
)
async def test_http_missing_or_bad_authorization_is_refused_without_backend_request(
    respx_mock, header
):
    login = respx_mock.post("/api/auth/login")
    execute = respx_mock.post("/api/agent/tools/execute")
    async with http_mode_client() as (client, _):
        result = await mcp_call(client, "search_knowledge", {"query": "q"}, header)
    assert result["isError"] is True
    assert "Authorization" in result["content"][0]["text"]
    assert execute.call_count == 0 and login.call_count == 0


@pytest.mark.respx(base_url=BACKEND, assert_all_called=False)
async def test_http_mode_ignores_the_operators_env_token(respx_mock):
    execute = respx_mock.post("/api/agent/tools/execute")
    async with http_mode_client(user_token="operators-own-token") as (client, _):
        result = await mcp_call(client, "search_knowledge", {"query": "q"}, None)
    assert result["isError"] is True and execute.call_count == 0


@pytest.mark.respx(base_url=BACKEND, assert_all_called=False)
@pytest.mark.parametrize("status", [401, 403])
async def test_http_rejected_token_errors_without_relogin(respx_mock, status):
    login = respx_mock.post("/api/auth/login")
    execute = respx_mock.post("/api/agent/tools/execute").mock(return_value=httpx.Response(status))
    async with http_mode_client() as (client, _):
        result = await mcp_call(client, "search_knowledge", {"query": "q"}, TOKEN_A)
    assert result["isError"] is True
    assert "rejected your token" in result["content"][0]["text"] or "rejected" in str(result)
    assert execute.call_count == 1 and login.call_count == 0


# --- identity cannot be chosen through arguments --------------------------------------------


@pytest.mark.respx(base_url=BACKEND, assert_all_called=False)
async def test_identity_arguments_are_dropped_and_do_not_change_the_token(respx_mock):
    execute = respx_mock.post("/api/agent/tools/execute").mock(
        return_value=execute_ok("search_knowledge")
    )
    sneaky = {
        "tenant": "t9",
        "tenant_id": "t9",
        "tenantId": "t9",
        "user": "admin",
        "user_id": "1",
        "userId": "1",
        "group": "admins",
        "group_id": "7",
        "authorization": "Bearer stolen",
        "token": "stolen",
    }
    async with http_mode_client() as (client, _):
        await mcp_call(client, "search_knowledge", {"query": "q", **sneaky}, TOKEN_A)
        await mcp_call(client, "list_documents", sneaky, TOKEN_B)
        await mcp_call(client, "get_document_status", {"document_id": "d", **sneaky}, TOKEN_A)
    assert _auth_headers(execute) == [f"Bearer {TOKEN_A}", f"Bearer {TOKEN_B}", f"Bearer {TOKEN_A}"]
    for call in execute.calls:
        sent = json.loads(call.request.content)["calls"][0]["arguments"]
        assert not set(sent) & set(sneaky), sent


# --- degraded mode: shared service account --------------------------------------------------


def _sa_kwargs() -> dict:
    return {
        "allow_shared_service_account": True,
        "service_username": "svc-mcp",
        "service_password": SA_LOGIN_PASSWORD,
    }


@pytest.mark.respx(base_url=BACKEND, assert_all_called=False)
async def test_degraded_mode_off_cannot_be_triggered_by_any_request(respx_mock):
    login = respx_mock.post("/api/auth/login")
    execute = respx_mock.post("/api/agent/tools/execute")
    async with http_mode_client() as (client, _):
        for header in (None, "Bearer", "Bearer a b", "Basic x"):
            result = await mcp_call(client, "search_knowledge", {"query": "q"}, header)
            assert result["isError"] is True
    assert login.call_count == 0 and execute.call_count == 0


def test_service_credentials_without_the_enable_flag_are_rejected_at_startup():
    with pytest.raises(ValidationError, match="ALLOW_SHARED_SERVICE_ACCOUNT"):
        Settings(service_username="svc", service_password="pw")


def test_enable_flag_without_credentials_is_rejected_at_startup():
    with pytest.raises(ValidationError, match="requires"):
        Settings(allow_shared_service_account=True)


@pytest.mark.respx(base_url=BACKEND, assert_all_called=False)
async def test_degraded_mode_on_serves_tokenless_calls_as_service_account_with_warning(
    respx_mock, caplog
):
    respx_mock.post("/api/auth/login").mock(
        return_value=httpx.Response(200, json={"data": {"token": "sa-jwt", "expiresIn": 3600}})
    )
    execute = respx_mock.post("/api/agent/tools/execute").mock(
        return_value=execute_ok("search_knowledge")
    )
    with caplog.at_level(logging.WARNING, logger="rag_mcp_server"):
        async with http_mode_client(**_sa_kwargs()) as (client, _):
            result = await mcp_call(client, "search_knowledge", {"query": "q"}, None)
    assert not result.get("isError")
    assert _auth_headers(execute) == ["Bearer sa-jwt"]
    text = caplog.text
    assert text.count("DEGRADED MODE ENABLED") == 1  # startup
    assert "SHARED service account" in text  # per-use
    assert SA_LOGIN_PASSWORD not in text and "sa-jwt" not in text


@pytest.mark.respx(base_url=BACKEND, assert_all_called=False)
@pytest.mark.parametrize("status", [401, 403])
async def test_degraded_mode_never_masks_a_rejected_caller_token(respx_mock, status):
    login = respx_mock.post("/api/auth/login")
    execute = respx_mock.post("/api/agent/tools/execute").mock(return_value=httpx.Response(status))
    async with http_mode_client(**_sa_kwargs()) as (client, _):
        result = await mcp_call(client, "search_knowledge", {"query": "q"}, TOKEN_A)
    assert result["isError"] is True
    assert login.call_count == 0 and _auth_headers(execute) == [f"Bearer {TOKEN_A}"]


@pytest.mark.respx(base_url=BACKEND, assert_all_called=False)
async def test_degraded_mode_never_masks_a_malformed_token(respx_mock):
    login = respx_mock.post("/api/auth/login")
    execute = respx_mock.post("/api/agent/tools/execute")
    async with http_mode_client(**_sa_kwargs()) as (client, _):
        result = await mcp_call(client, "search_knowledge", {"query": "q"}, "Bearer a b")
    assert result["isError"] is True
    assert login.call_count == 0 and execute.call_count == 0
