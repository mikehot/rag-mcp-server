from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from rag_mcp_server.backend_client import (
    BackendUnavailableError,
    ToolCallResult,
)
from rag_mcp_server.server import create_server


@dataclass
class FakeBackendClient:
    """A stand-in for BackendClient: no HTTP, just a programmable call_tool."""

    handler: Callable[[str, dict[str, Any]], ToolCallResult]
    calls: list[tuple[str, dict[str, Any]]] | None = None

    def __post_init__(self) -> None:
        self.calls = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> ToolCallResult:
        self.calls.append((name, arguments))
        return self.handler(name, arguments)


async def test_lists_exactly_the_three_read_only_tools(settings):
    server = create_server(
        settings, client=FakeBackendClient(lambda n, a: ToolCallResult(n, True, {}, None))
    )
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert names == {"search_knowledge", "list_documents", "get_document_status"}


async def test_search_knowledge_requires_query(settings):
    server = create_server(
        settings, client=FakeBackendClient(lambda n, a: ToolCallResult(n, True, {}, None))
    )
    tools = {t.name: t for t in await server.list_tools()}
    schema = tools["search_knowledge"].input_schema
    assert schema["required"] == ["query"]


async def test_get_document_status_requires_document_id(settings):
    server = create_server(
        settings, client=FakeBackendClient(lambda n, a: ToolCallResult(n, True, {}, None))
    )
    tools = {t.name: t for t in await server.list_tools()}
    schema = tools["get_document_status"].input_schema
    assert schema["required"] == ["document_id"]


async def test_list_documents_has_no_required_fields(settings):
    server = create_server(
        settings, client=FakeBackendClient(lambda n, a: ToolCallResult(n, True, {}, None))
    )
    tools = {t.name: t for t in await server.list_tools()}
    schema = tools["list_documents"].input_schema
    assert schema.get("required", []) == []


async def test_search_knowledge_proxies_arguments_and_returns_data(settings):
    fake = FakeBackendClient(
        lambda n, a: ToolCallResult(
            n, True, {"found": True, "sources": [{"filename": "a.md"}]}, None
        )
    )
    server = create_server(settings, client=fake)
    result = await server.call_tool("search_knowledge", {"query": "leave policy"})
    assert result.is_error is False
    assert result.structured_content == {"found": True, "sources": [{"filename": "a.md"}]}
    assert fake.calls == [("search_knowledge", {"query": "leave policy"})]


async def test_search_knowledge_omits_knowledge_base_id_when_not_given(settings):
    fake = FakeBackendClient(
        lambda n, a: ToolCallResult(n, True, {"found": False, "sources": []}, None)
    )
    server = create_server(settings, client=fake)
    await server.call_tool("search_knowledge", {"query": "x"})
    assert fake.calls[0][1] == {"query": "x"}  # no knowledgeBaseId key at all


async def test_search_knowledge_forwards_knowledge_base_id_when_given(settings):
    fake = FakeBackendClient(
        lambda n, a: ToolCallResult(n, True, {"found": False, "sources": []}, None)
    )
    server = create_server(settings, client=fake)
    await server.call_tool("search_knowledge", {"query": "x", "knowledge_base_id": "kb-1"})
    assert fake.calls[0][1] == {"query": "x", "knowledgeBaseId": "kb-1"}


async def test_permission_denied_raises_a_readable_tool_error(settings):
    fake = FakeBackendClient(lambda n, a: ToolCallResult(n, False, None, "PERMISSION_DENIED"))
    server = create_server(settings, client=fake)
    with pytest.raises(ToolError, match="cannot access"):
        await server.call_tool("get_document_status", {"document_id": "doc-1"})


async def test_backend_unavailable_raises_a_safe_tool_error_without_leaking_details(settings):
    def handler(n: str, a: dict[str, Any]) -> ToolCallResult:
        raise BackendUnavailableError(
            "http://internal-host:8080/api/agent/tools/execute unreachable"
        )

    server = create_server(settings, client=FakeBackendClient(handler))
    with pytest.raises(ToolError) as excinfo:
        await server.call_tool("search_knowledge", {"query": "x"})
    # The internal URL must not leak into the message shown to the calling model.
    assert "internal-host" not in str(excinfo.value)


async def test_unknown_failure_reason_still_produces_a_tool_error(settings):
    fake = FakeBackendClient(lambda n, a: ToolCallResult(n, False, None, "SOMETHING_NEW"))
    server = create_server(settings, client=fake)
    with pytest.raises(ToolError, match="SOMETHING_NEW"):
        await server.call_tool("list_documents", {})
