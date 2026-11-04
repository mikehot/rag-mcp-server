from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from rag_mcp_server.backend_client import (
    BackendAuthError,
    BackendUnavailableError,
    ToolCallResult,
)
from rag_mcp_server.server import create_server


@dataclass
class FakeBackendClient:
    """A stand-in for BackendClient: no HTTP, just a programmable call_tool."""

    handler: Callable[[str, dict[str, Any]], ToolCallResult]
    calls: list[tuple[str, dict[str, Any], str | None]] = field(default_factory=list)

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, token: str | None
    ) -> ToolCallResult:
        self.calls.append((name, arguments, token))
        return self.handler(name, arguments)


def _ok(n: str, a: dict[str, Any]) -> ToolCallResult:
    return ToolCallResult(n, True, {}, None)


async def test_lists_exactly_the_three_read_only_tools(settings):
    server = create_server(settings, client=FakeBackendClient(_ok))
    assert {t.name for t in await server.list_tools()} == {
        "search_knowledge",
        "list_documents",
        "get_document_status",
    }


async def test_tool_schemas_expose_no_identity_parameters(settings):
    server = create_server(settings, client=FakeBackendClient(_ok))
    tools = {t.name: t for t in await server.list_tools()}
    assert tools["search_knowledge"].input_schema["required"] == ["query"]
    assert tools["get_document_status"].input_schema["required"] == ["document_id"]
    assert tools["list_documents"].input_schema.get("required", []) == []
    for tool in tools.values():
        props = set(tool.input_schema["properties"])
        assert not props & {"tenant", "tenant_id", "user", "user_id", "group", "group_id", "ctx"}


async def test_search_knowledge_proxies_arguments_and_the_users_token(settings):
    fake = FakeBackendClient(lambda n, a: ToolCallResult(n, True, {"found": True}, None))
    server = create_server(settings, client=fake)
    result = await server.call_tool("search_knowledge", {"query": "leave policy"})
    assert result.is_error is False and result.structured_content == {"found": True}
    assert fake.calls == [("search_knowledge", {"query": "leave policy"}, "token-of-user-A.aaa")]


async def test_knowledge_base_id_is_forwarded_only_when_given(settings):
    fake = FakeBackendClient(_ok)
    server = create_server(settings, client=fake)
    await server.call_tool("search_knowledge", {"query": "x"})
    await server.call_tool("search_knowledge", {"query": "x", "knowledge_base_id": "kb-1"})
    assert fake.calls[0][1] == {"query": "x"}
    assert fake.calls[1][1] == {"query": "x", "knowledgeBaseId": "kb-1"}


async def test_permission_denied_raises_a_readable_tool_error(settings):
    fake = FakeBackendClient(lambda n, a: ToolCallResult(n, False, None, "PERMISSION_DENIED"))
    server = create_server(settings, client=fake)
    with pytest.raises(ToolError, match="do not have access"):
        await server.call_tool("get_document_status", {"document_id": "doc-1"})


async def test_backend_unavailable_does_not_leak_details(settings):
    def handler(n: str, a: dict[str, Any]) -> ToolCallResult:
        raise BackendUnavailableError("http://internal-host:8080 unreachable")

    server = create_server(settings, client=FakeBackendClient(handler))
    with pytest.raises(ToolError) as excinfo:
        await server.call_tool("search_knowledge", {"query": "x"})
    assert "internal-host" not in str(excinfo.value)


async def test_unknown_failure_reason_still_produces_a_tool_error(settings):
    fake = FakeBackendClient(lambda n, a: ToolCallResult(n, False, None, "SOMETHING_NEW"))
    server = create_server(settings, client=fake)
    with pytest.raises(ToolError, match="SOMETHING_NEW"):
        await server.call_tool("list_documents", {})


async def test_rejected_caller_token_gives_actionable_error(settings):
    def handler(n: str, a: dict[str, Any]) -> ToolCallResult:
        raise BackendAuthError("rejected")

    server = create_server(settings, client=FakeBackendClient(handler))
    with pytest.raises(ToolError, match="expired, revoked, or invalid"):
        await server.call_tool("search_knowledge", {"query": "x"})
