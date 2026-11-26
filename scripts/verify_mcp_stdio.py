"""Verify real MCP stdio handshake (initialize + tools/list, optional live tool call)."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def run_verify(backend_url: str, token: str, call_live: bool) -> None:
    env = dict(os.environ)
    env["RAG_MCP_BACKEND_BASE_URL"] = backend_url
    env["RAG_USER_TOKEN"] = token

    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "rag_mcp_server"],
        env=env,
    )
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        info = init.server_info
        print(f"[OK] initialize: server={info.name} version={info.version}")

        tools_resp = await session.list_tools()
        names = [t.name for t in tools_resp.tools]
        print(f"[OK] tools/list: {names}")
        expected = {"search_knowledge", "list_documents", "get_document_status"}
        if not expected.issubset(set(names)):
            raise AssertionError(f"missing expected tools: {expected - set(names)}")

        if call_live:
            result = await session.call_tool("list_documents", {})
            print(f"[OK] tools/call(list_documents): isError={result.isError}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--backend-url",
        default=os.getenv("RAG_MCP_BACKEND_BASE_URL", "http://localhost:8080"),
    )
    parser.add_argument("--token", default=os.getenv("RAG_USER_TOKEN", "smoke-test-token"))
    parser.add_argument(
        "--live",
        action="store_true",
        help="Also execute list_documents against live backend",
    )
    args = parser.parse_args()
    asyncio.run(run_verify(args.backend_url, args.token, args.live))


if __name__ == "__main__":
    main()
