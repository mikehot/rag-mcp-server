from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from rag_mcp_server.config import Settings
from rag_mcp_server.server import create_server

EXECUTE_URL = "http://backend.test/api/agent/tools/execute"
LOGIN_URL = "http://backend.test/api/auth/login"

TOKEN_A = "token-of-user-A.aaa"
TOKEN_B = "token-of-user-B.bbb"


@pytest.fixture
def settings() -> Settings:
    """stdio mode with a user token -- the default per-caller configuration."""
    return Settings(backend_base_url="http://backend.test", user_token=TOKEN_A)


def execute_ok(name: str, data: dict | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "code": 0,
            "data": {
                "requestId": "11111111-1111-1111-1111-111111111111",
                "results": [{"name": name, "success": True, "data": data or {}}],
            },
        },
    )


@asynccontextmanager
async def http_mode_client(**overrides: Any) -> AsyncIterator[tuple[httpx.AsyncClient, Settings]]:
    """The real streamable-HTTP ASGI app, driven in-process (no sockets)."""
    cfg = Settings(transport="streamable-http", backend_base_url="http://backend.test", **overrides)
    app = create_server(cfg).streamable_http_app(stateless_http=True, json_response=True)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as c:
            yield c, cfg


async def mcp_call(
    client: httpx.AsyncClient, tool: str, arguments: dict, token: str | None, req_id: int = 1
) -> dict:
    headers = {"Accept": "application/json, text/event-stream"}
    if token is not None:
        headers["Authorization"] = token if token.startswith("Bearer") else f"Bearer {token}"
    response = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]
