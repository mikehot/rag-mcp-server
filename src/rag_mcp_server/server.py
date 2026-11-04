"""Builds the MCP server: three read-only tools, each a thin proxy to the Java
backend's `/api/agent/tools/execute`, run as the *calling user*. No reasoning, no LLM call, no
memory happens here -- see README for why.

Identity rules (docs/DESIGN.md): the only identity input is the caller's bearer token --
`RAG_USER_TOKEN` over stdio, the per-request `Authorization` header over streamable-HTTP.
Tool arguments never influence identity; the tool signatures simply have no tenant/user/group
parameters, and the handlers build the backend arguments explicitly, so any extra argument a
caller sends (tenant_id, user_id, ...) is dropped and never forwarded.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .auth import CredentialError, token_from_authorization_header, validate_token
from .backend_client import (
    BackendAuthError,
    BackendClient,
    BackendProtocolError,
    BackendUnavailableError,
    ToolCallResult,
)
from .config import Settings

log = logging.getLogger("rag_mcp_server")

_FAILURE_MESSAGES = {
    "TOOL_NOT_FOUND": "The backend does not recognize this tool name.",
    "INVALID_ARGUMENTS": "The arguments were rejected by the backend (bad shape, bad UUID, or "
    "an unknown field).",
    "PERMISSION_DENIED": "You do not have access to that resource.",
    "TIMEOUT": "The backend's model/embedding provider timed out handling this call.",
    "PROVIDER_ERROR": "The backend's model/embedding provider returned an error.",
    "INTERNAL_ERROR": "The backend failed internally while handling this call.",
}

_HOW_TO_FIX = {
    "stdio": "Set RAG_USER_TOKEN (your own bearer token) in this server's environment, e.g. in "
    "the 'env' block of your MCP client config, then restart the client.",
    "streamable-http": "Send your own token as 'Authorization: Bearer <token>' on every request.",
}


def _resolve_token(settings: Settings, ctx: Context) -> str | None:
    """Return the calling user's token, or None to use the shared service account.

    None is returned only when NO token was supplied and the degraded mode is explicitly
    enabled. A supplied-but-malformed token is always an error, never a fallback.
    """
    try:
        if settings.transport == "stdio":
            raw = settings.user_token.get_secret_value() if settings.user_token else None
            return validate_token(raw)
        # HTTP: identity is per request. Never read RAG_USER_TOKEN here, or every remote caller
        # would silently share the operator's identity.
        try:
            header = (ctx.headers or {}).get("authorization")
        except ValueError:  # no request context (not inside an HTTP request)
            header = None
        return token_from_authorization_header(header)
    except CredentialError as exc:
        if exc.reason == "missing" and settings.allow_shared_service_account:
            log.warning(
                "DEGRADED MODE: request carried no user token; serving it as the SHARED service "
                "account (RAG_MCP_ALLOW_SHARED_SERVICE_ACCOUNT=true)."
            )
            return None
        log.info("tool call refused: credential %s", exc.reason)
        raise ToolError(f"{exc} {_HOW_TO_FIX[settings.transport]}") from None


async def _invoke(
    backend: BackendClient,
    settings: Settings,
    ctx: Context,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    token = _resolve_token(settings, ctx)
    try:
        result: ToolCallResult = await backend.call_tool(name, arguments, token=token)
    except BackendUnavailableError:
        raise ToolError(
            "The knowledge-base backend could not be reached. Try again shortly."
        ) from None
    except BackendAuthError as exc:
        if exc.service_account:
            log.error("shared service account was rejected by the backend")
            raise ToolError(
                "The shared service account (degraded mode) was rejected by the backend. "
                "Ask the operator to check its credentials."
            ) from None
        log.info("tool call refused: backend rejected the caller's token")
        raise ToolError(
            "The backend rejected your token (expired, revoked, or invalid). Obtain a new "
            f"token. {_HOW_TO_FIX[settings.transport]}"
        ) from None
    except BackendProtocolError:
        raise ToolError("The knowledge-base backend returned an unexpected response.") from None
    if not result.success:
        reason = result.failure_reason or "UNKNOWN"
        raise ToolError(_FAILURE_MESSAGES.get(reason, f"Tool call failed: {reason}"))
    return result.data if isinstance(result.data, dict) else {}


def create_server(settings: Settings, client: BackendClient | None = None) -> MCPServer:
    """Build the MCP server. Pass `client` to inject a fake backend in tests;
    otherwise a real `BackendClient` is built from `settings`."""
    backend = client or BackendClient(settings)
    if settings.allow_shared_service_account:
        log.warning(
            "DEGRADED MODE ENABLED: callers without a token will share the service account "
            "identity. See SECURITY.md."
        )
    server: MCPServer = MCPServer(
        name="rag-knowledge-base",
        version="0.2.0",
        instructions=(
            "Read-only access to an enterprise knowledge base, run as the calling user: results "
            "are bounded to what the user's own token may see. No documents are modified "
            "through this server."
        ),
    )

    @server.tool()
    async def search_knowledge(
        ctx: Context, query: str, knowledge_base_id: str | None = None
    ) -> dict[str, Any]:
        """Search ACL-filtered knowledge-base chunks for a query. Does not generate an answer --
        returns the matching chunks (filename, locator, snippet, similarity) for the caller to
        read and cite itself."""
        arguments: dict[str, Any] = {"query": query}
        if knowledge_base_id is not None:
            arguments["knowledgeBaseId"] = knowledge_base_id
        return await _invoke(backend, settings, ctx, "search_knowledge", arguments)

    @server.tool()
    async def list_documents(
        ctx: Context, status: str | None = None, knowledge_base_id: str | None = None
    ) -> dict[str, Any]:
        """List ACL-filtered document metadata and processing status. `status`, if given, must be
        one of: processing, ready, failed."""
        arguments: dict[str, Any] = {}
        if status is not None:
            arguments["status"] = status
        if knowledge_base_id is not None:
            arguments["knowledgeBaseId"] = knowledge_base_id
        return await _invoke(backend, settings, ctx, "list_documents", arguments)

    @server.tool()
    async def get_document_status(ctx: Context, document_id: str) -> dict[str, Any]:
        """Read the processing status of one ACL-visible document. Never returns file content."""
        return await _invoke(
            backend, settings, ctx, "get_document_status", {"documentId": document_id}
        )

    return server
