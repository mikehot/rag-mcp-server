"""Standalone MCP server for the rag-knowledge-base project.

Exposes the backend's three read-only tools (search_knowledge, list_documents,
get_document_status) over the real MCP protocol, so any MCP client -- Claude
Desktop, Claude Code, or another agent framework -- can use them directly,
not just the project's own Python agent. See README.md for the architecture
and SECURITY.md for the auth model and what this server does and does not do.
"""

from .config import load_settings
from .server import create_server

__all__ = ["main"]


def main() -> None:
    settings = load_settings()
    server = create_server(settings)
    server.run(transport="stdio")
