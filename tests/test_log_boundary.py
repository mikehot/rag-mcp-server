"""Logs and error text must never contain tokens, document text, or user names."""

from __future__ import annotations

import logging

import httpx
import pytest

from rag_mcp_server.config import Settings
from rag_mcp_server.server import create_server

from .conftest import TOKEN_A, execute_ok, http_mode_client, mcp_call

BACKEND = "http://backend.test"
DOC_TEXT = "CONFIDENTIAL-QUARTERLY-NUMBERS-4242"
USERNAME = "alice.example"
QUERY = "secret-query-text-9191"


def _assert_clean(blob: str, *, allow_query: bool = False) -> None:
    for needle in (TOKEN_A, DOC_TEXT, USERNAME):
        assert needle not in blob, f"{needle!r} leaked"
    if not allow_query:
        assert QUERY not in blob


@pytest.mark.respx(base_url=BACKEND, assert_all_called=False)
@pytest.mark.parametrize("mode", ["stdio", "http"])
@pytest.mark.parametrize(
    "backend",
    [
        httpx.Response(401, text=f"{DOC_TEXT} {USERNAME}"),
        httpx.Response(403, text=f"{DOC_TEXT} {USERNAME}"),
        httpx.Response(500, text=f"{DOC_TEXT} {USERNAME}"),
        httpx.Response(200, text=f"not json {DOC_TEXT} {USERNAME}"),
        httpx.ConnectError(f"refused {TOKEN_A}"),
        "success",
    ],
    ids=["401", "403", "500", "bad-json", "network", "success"],
)
async def test_no_token_body_or_username_in_logs_or_errors(respx_mock, caplog, mode, backend):
    if backend == "success":
        reply = execute_ok(
            "search_knowledge", {"sources": [{"snippet": DOC_TEXT, "owner": USERNAME}]}
        )
        route = respx_mock.post("/api/agent/tools/execute").mock(return_value=reply)
    elif isinstance(backend, Exception):
        route = respx_mock.post("/api/agent/tools/execute").mock(side_effect=backend)
    else:
        route = respx_mock.post("/api/agent/tools/execute").mock(return_value=backend)

    with caplog.at_level(logging.DEBUG):
        if mode == "stdio":
            server = create_server(Settings(backend_base_url=BACKEND, user_token=TOKEN_A))
            try:
                result = await server.call_tool("search_knowledge", {"query": QUERY})
                visible = str(result.structured_content)
            except Exception as exc:  # ToolError text is what the model/user sees
                visible = str(exc)
        else:
            async with http_mode_client() as (client, _):
                result = await mcp_call(client, "search_knowledge", {"query": QUERY}, TOKEN_A)
            visible = str(result)
    assert route.call_count == 1

    _assert_clean(caplog.text)  # includes SDK, httpx, uvicorn-free ASGI loggers, and ours
    if backend != "success":
        _assert_clean(visible, allow_query=True)  # the error shown to the user is token/body-free
    else:
        assert DOC_TEXT in visible  # the tool result itself legitimately carries the content


@pytest.mark.respx(base_url=BACKEND, assert_all_called=False)
async def test_refusals_for_missing_or_malformed_tokens_log_no_input(respx_mock, caplog):
    bad = "Bearer leaked-looking-value with spaces"
    with caplog.at_level(logging.DEBUG):
        async with http_mode_client() as (client, _):
            result = await mcp_call(client, "search_knowledge", {"query": QUERY}, bad)
    assert result["isError"] is True
    assert "leaked-looking-value" not in caplog.text and QUERY not in caplog.text
    assert "leaked-looking-value" not in str(result)
