"""Builds the MCP server: three read-only tools, each a thin proxy to the Java
backend's `/api/agent/tools/execute`. No reasoning, no LLM call, no memory happens
here -- see README for why.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .backend_client import (
    BackendAuthError,
    BackendClient,
    BackendProtocolError,
    BackendUnavailableError,
    ToolCallResult,
)
from .config import Settings

_FAILURE_MESSAGES = {
    "TOOL_NOT_FOUND": "The backend does not recognize this tool name.",
    "INVALID_ARGUMENTS": "The arguments were rejected by the backend (bad shape, bad UUID, or "
    "an unknown field).",
    "PERMISSION_DENIED": "The service account this server runs as cannot access that resource.",
    "TIMEOUT": "The backend's model/embedding provider timed out handling this call.",
    "PROVIDER_ERROR": "The backend's model/embedding provider returned an error.",
    "INTERNAL_ERROR": "The backend failed internally while handling this call.",
}


async def _invoke(client: BackendClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        result: ToolCallResult = await client.call_tool(name, arguments)
    except BackendUnavailableError as exc:
        raise ToolError(
            "The knowledge-base backend could not be reached. Try again shortly."
        ) from exc
    except BackendAuthError as exc:
        raise ToolError(
            "The knowledge-base backend rejected this server's service-account credentials."
        ) from exc
    except BackendProtocolError as exc:
        raise ToolError("The knowledge-base backend returned an unexpected response.") from exc
    if not result.success:
        reason = result.failure_reason or "UNKNOWN"
        raise ToolError(_FAILURE_MESSAGES.get(reason, f"Tool call failed: {reason}"))
    return result.data if isinstance(result.data, dict) else {}


def create_server(settings: Settings, client: BackendClient | None = None) -> MCPServer:
    """Build the MCP server. Pass `client` to inject a fake backend in tests;
    otherwise a real `BackendClient` is built from `settings`."""
    backend = client or BackendClient(settings)
    server: MCPServer = MCPServer(
        name="rag-knowledge-base",
        version="0.1.0",
        instructions=(
            "Read-only access to one enterprise knowledge base. Every call runs as a fixed "
            "service account (see SECURITY.md) -- results are bounded to what that account "
            "can see, not the end user's own permissions. No documents are modified through "
            "this server."
        ),
    )

    @server.tool()
    async def search_knowledge(query: str, knowledge_base_id: str | None = None) -> dict[str, Any]:
        """Search ACL-filtered knowledge-base chunks for a query. Does not generate an answer --
        returns the matching chunks (filename, locator, snippet, similarity) for the caller to
        read and cite itself."""
        arguments: dict[str, Any] = {"query": query}
        if knowledge_base_id is not None:
            arguments["knowledgeBaseId"] = knowledge_base_id
        return await _invoke(backend, "search_knowledge", arguments)

    @server.tool()
    async def list_documents(
        status: str | None = None, knowledge_base_id: str | None = None
    ) -> dict[str, Any]:
        """List ACL-filtered document metadata and processing status. `status`, if given, must be
        one of: processing, ready, failed."""
        arguments: dict[str, Any] = {}
        if status is not None:
            arguments["status"] = status
        if knowledge_base_id is not None:
            arguments["knowledgeBaseId"] = knowledge_base_id
        return await _invoke(backend, "list_documents", arguments)

    @server.tool()
    async def get_document_status(document_id: str) -> dict[str, Any]:
        """Read the processing status of one ACL-visible document. Never returns file content."""
        return await _invoke(backend, "get_document_status", {"documentId": document_id})

    return server
