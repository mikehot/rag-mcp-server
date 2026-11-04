"""Standalone MCP server for the rag-knowledge-base project.

Exposes the backend's three read-only tools (search_knowledge, list_documents,
get_document_status) over the real MCP protocol, so any MCP client -- Claude
Desktop, Claude Code, or another agent framework -- can use them directly,
not just the project's own Python agent. Every backend call runs as the real caller
(per-caller bearer-token passthrough). See README.md for the architecture
and SECURITY.md for the auth model and what this server does and does not do.
"""

import logging

from .config import load_settings
from .server import create_server

__all__ = ["main"]


def main() -> None:
    settings = load_settings()
    logging.basicConfig(level=logging.INFO)  # stderr; stdout is the stdio protocol channel
    # httpx logs every request URL at INFO; keep this process quiet by default.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    server = create_server(settings)
    if settings.transport == "stdio":
        if settings.user_token is None and not settings.allow_shared_service_account:
            logging.getLogger("rag_mcp_server").warning(
                "RAG_USER_TOKEN is not set: every tool call will be refused until it is."
            )
        server.run(transport="stdio")
    else:
        log = logging.getLogger("rag_mcp_server")
        if settings.user_token is not None:
            log.warning(
                "RAG_USER_TOKEN is ignored in streamable-http mode (identity is per request)."
            )
        if settings.http_host not in ("127.0.0.1", "localhost", "::1"):
            log.warning(
                "Listening on a non-loopback address without TLS or MCP-level auth: put this "
                "behind a TLS-terminating reverse proxy. See SECURITY.md."
            )
        server.run(
            transport="streamable-http",
            host=settings.http_host,
            port=settings.http_port,
            stateless_http=True,
            json_response=True,
        )
