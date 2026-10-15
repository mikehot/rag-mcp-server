# rag-mcp-server

A standalone, real MCP server that puts the [rag-knowledge-base](https://github.com/mikehot/rag-knowledge-base)
project's knowledge base behind the standard [Model Context Protocol](https://modelcontextprotocol.io/),
so any MCP client — Claude Desktop, Claude Code, or another agent framework — can query it directly,
without going through the project's own Flutter app or [Python agent](https://github.com/mikehot/rag-agent-python).

> This server has no "brain": it never calls an LLM and makes no decisions about what to search or how to
> answer. It exposes three read-only tools and nothing else; whatever connects to it (another AI) does the
> reasoning. See [SECURITY.md](SECURITY.md) for the auth model and exactly what it holds and logs.

## Where this sits

```
MCP client (Claude Desktop / Claude Code / any MCP host)
        │  MCP protocol over stdio
        ▼
rag-mcp-server (this repo)             ── a single service account's JWT, in memory only
        │  POST /api/agent/tools/execute, Authorization: Bearer <jwt>
        ▼
rag-knowledge-base Java backend        ── owns retrieval, ACL, audit (unchanged)
        │
        ▼
PostgreSQL + pgvector
```

The Java backend already has a minimal, explicitly non-conformant "MCP-flavored" JSON-RPC adapter at
`POST /mcp` (see its `McpAdapterService`, which documents itself as not implementing sessions, the
`initialize` handshake, or any transport beyond plain HTTP POST). This server does not call that endpoint.
It calls the backend's plain `POST /api/agent/tools/execute` REST endpoint directly and does the *real*
MCP protocol work (handshake, capability negotiation, stdio framing) itself, using the official
[`mcp` Python SDK](https://pypi.org/project/mcp/) (v2). Re-wrapping the backend's own JSON-RPC inside this
server's JSON-RPC would add a layer of translation with no benefit, since this process is already the
MCP-compliant layer.

The three tools (unchanged names and arguments from the backend's own `AgentToolService`):

| Tool | Arguments | What it returns |
|---|---|---|
| `search_knowledge` | `query` (required), `knowledge_base_id` (optional) | ACL-filtered chunk matches — no answer is generated |
| `list_documents` | `status`, `knowledge_base_id` (both optional) | Document metadata and processing status |
| `get_document_status` | `document_id` (required) | One document's status, never its file content |

## Setup

### 1. Create a dedicated service account on the Java backend

Don't reuse an admin account — see [SECURITY.md](SECURITY.md) for why. As a `SYSTEM_ADMIN` on the backend:

```bash
curl -X POST "$BASE_URL/api/admin/users" -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"username":"svc-mcp-readonly","password":"<generate one>","roleCodes":["EMPLOYEE"]}'

# Grant it only the knowledge bases this server should expose:
curl -X POST "$BASE_URL/api/admin/knowledge-bases/<kbId>/memberships" \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"principalType":"USER","principalId":"<new user id>","permission":"READ"}'
```

Verified against a live backend on 2026-10-15 (see the commit history / session notes) — this exact pair of
calls, followed by a real `search_knowledge` and `list_documents` call through this server, returned real
ACL-scoped results.

### 2. Configure this server

```bash
cp .env.example .env
# edit .env: RAG_MCP_BACKEND_BASE_URL, RAG_MCP_SERVICE_USERNAME, RAG_MCP_SERVICE_PASSWORD
uv sync
```

### 3. Point an MCP client at it

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "rag-knowledge-base": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/rag-mcp-server", "rag-mcp-server"],
      "env": {
        "RAG_MCP_BACKEND_BASE_URL": "http://localhost:8080",
        "RAG_MCP_SERVICE_USERNAME": "svc-mcp-readonly",
        "RAG_MCP_SERVICE_PASSWORD": "<the password you generated>"
      }
    }
  }
}
```

Restart the client; it should list `search_knowledge`, `list_documents`, `get_document_status`.

## Development

```bash
uv sync                      # install
uv run pytest                # unit + contract tests (mocked backend, no live server needed)
uv run ruff check .          # lint
uv run ruff format .         # format
uv run rag-mcp-server        # run for real against RAG_MCP_BACKEND_BASE_URL (needs the Java backend up)
```

Tests never hit a real backend: `tests/test_backend_client.py` mocks the Java backend's HTTP endpoints
with `respx`; `tests/test_server_contract.py` injects a fake `BackendClient` and exercises the MCP server's
tool schemas and error mapping directly via `MCPServer.list_tools()` / `MCPServer.call_tool()`.

## Relationship to the other two repos

| Repo | Role |
|---|---|
| [rag-knowledge-base](https://github.com/mikehot/rag-knowledge-base) | Owns the data, retrieval, ACL, and the single-shot `/api/ask` Q&A system. This server is a thin client of its read-only tool endpoints, nothing more. |
| [rag-agent-python](https://github.com/mikehot/rag-agent-python) | A separate, complete multi-step Q&A agent (its own LLM calls, conversation memory, `decompose` policy) that happens to also call the Java backend's tools, via its own hand-rolled MCP-shaped client. Architecturally unrelated to this server beyond calling the same backend. |
| **rag-mcp-server** (this repo) | No LLM call, no reasoning, no memory. Just a protocol-compliant MCP server so external MCP clients can use the same read-only tools. |

## Status

v0.1, stdio transport only, single service-account auth, read-only. See
[SECURITY.md](SECURITY.md) for exactly what's out of scope and why.
