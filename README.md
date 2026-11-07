# rag-mcp-server

A standalone, real MCP server that puts the [rag-knowledge-base](https://github.com/mikehot/rag-knowledge-base)
project's knowledge base behind the standard [Model Context Protocol](https://modelcontextprotocol.io/),
so any MCP client — Claude Desktop, Claude Code, or another agent framework — can query it directly,
without going through the project's own Flutter app or [Python agent](https://github.com/mikehot/rag-agent-python).

> This server has no "brain": it never calls an LLM and makes no decisions about what to search or how to
> answer. It exposes three read-only tools and nothing else; whatever connects to it (another AI) does the
> reasoning. Every backend call runs as the real caller (per-caller bearer-token passthrough, same as
> [rag-agent-python](https://github.com/mikehot/rag-agent-python)). See [SECURITY.md](SECURITY.md) for the auth model
> and [docs/DESIGN.md](docs/DESIGN.md) for the rationale.

## Where this sits

```
MCP client (Claude Desktop / Claude Code / any MCP host)
        │  MCP protocol over stdio (or streamable-HTTP)
        ▼
rag-mcp-server (this repo)             ── no identity of its own; forwards the CALLER's own token
        │  POST /api/agent/tools/execute, Authorization: Bearer <caller's token>
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

### 1. Get your own backend token

Each user uses their **own** token for the rag-knowledge-base backend (the one you would use to call the
backend as yourself). What you can see through this server is exactly what that token can see. No service
account is needed.

### 2. Install

```bash
uv sync
```

### 3. Connect Claude Desktop (stdio)

`claude_desktop_config.json` -- every user puts **their own** token in **their own** config:

```json
{
  "mcpServers": {
    "rag-knowledge-base": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/rag-mcp-server", "rag-mcp-server"],
      "env": {
        "RAG_MCP_BACKEND_BASE_URL": "http://localhost:8080",
        "RAG_USER_TOKEN": "<your own bare token: no 'Bearer ' prefix, no quotes>"
      }
    }
  }
}
```

The config file stores the token in plaintext; protect it like a password and never commit it. Restart
Claude Desktop after editing. When the token expires, replace it and restart again.

#### Live verification checklist (against a running backend)

Automated tests in `tests/test_identity.py` verify these six properties in-process with `respx` mocks.
When connecting Claude Desktop to a live Java backend, verify end-to-end with:

1. Restart Claude Desktop; the server should list `search_knowledge`, `list_documents`, `get_document_status`.
2. Ask it to run `list_documents`. Expect only documents your own account can see.
3. With a second account's token in a second config (or a second machine), repeat and confirm the results differ
   according to that account's ACL, and that the backend audit/log shows each real user.
4. Remove `RAG_USER_TOKEN`, restart: every tool call should fail with a message asking you to set it; the backend
   should see no request from this server.
5. Put a deliberately expired/invalid token: expect "The backend rejected your token...", not a fallback.
6. Ask the model to search "as user admin / tenant X": results must not change (identity comes from the token).

### Optional: streamable-HTTP mode

```bash
RAG_MCP_TRANSPORT=streamable-http RAG_MCP_BACKEND_BASE_URL=http://localhost:8080 uv run rag-mcp-server
# listens on 127.0.0.1:8000, endpoint /mcp (RAG_MCP_HTTP_HOST / RAG_MCP_HTTP_PORT to change)
```

Every request must carry `Authorization: Bearer <that caller's token>`; it is forwarded for that request only.
`RAG_USER_TOKEN` is ignored in this mode. The server does no TLS or MCP-level auth: keep it on loopback or
behind a TLS-terminating reverse proxy (see [SECURITY.md](SECURITY.md)).

### Migrating from the service-account mode (v0.1)

v0.1 logged in as `RAG_MCP_SERVICE_USERNAME` / `RAG_MCP_SERVICE_PASSWORD` and shared that identity with every
caller. v0.2:

1. Give each user their own token and set `RAG_USER_TOKEN` in their client config (stdio), or have each HTTP
   client send `Authorization: Bearer`.
2. **Remove** `RAG_MCP_SERVICE_USERNAME` / `RAG_MCP_SERVICE_PASSWORD`. If they are still set, startup fails on
   purpose (a leftover config must not be mistaken for an active, safe one).
3. Tool names and arguments are unchanged. Behaviour change: results now follow the caller's own permissions
   instead of the service account's; a user with no access to something that used to be visible through the
   service account will no longer see it.
4. If you must keep the old behaviour temporarily, opt in explicitly:
   `RAG_MCP_ALLOW_SHARED_SERVICE_ACCOUNT=true` plus the two service-account variables. This serves only
   requests that carry **no** token, logs a warning at startup and on each such call, and is **not recommended**:
   all such callers share one identity and the backend audit cannot tell them apart. A supplied token that is
   expired or rejected is never replaced by the service account.

## Development

```bash
uv sync                      # install
uv run pytest                # unit + contract tests (mocked backend, no live server needed)
uv run ruff check .          # lint
uv run ruff format .         # format
uv run rag-mcp-server        # run for real against RAG_MCP_BACKEND_BASE_URL (needs the Java backend up)
```

Tests never hit a real backend: `tests/test_backend_client.py` and `tests/test_identity.py` mock the Java
backend's HTTP endpoints with `respx` (the latter drives the real streamable-HTTP ASGI app in-process);
`tests/test_server_contract.py` injects a fake `BackendClient`; `tests/test_log_boundary.py` captures logs
and asserts no token, response body or user name appears.

## Relationship to the other two repos

| Repo | Role |
|---|---|
| [rag-knowledge-base](https://github.com/mikehot/rag-knowledge-base) | Owns the data, retrieval, ACL, and the single-shot `/api/ask` Q&A system. This server is a thin client of its read-only tool endpoints, nothing more. |
| [rag-agent-python](https://github.com/mikehot/rag-agent-python) | A separate, complete multi-step Q&A agent (its own LLM calls, conversation memory, `decompose` policy) that happens to also call the Java backend's tools, via its own hand-rolled MCP-shaped client. Architecturally unrelated to this server beyond calling the same backend. |
| **rag-mcp-server** (this repo) | No LLM call, no reasoning, no memory. Just a protocol-compliant MCP server so external MCP clients can use the same read-only tools. |

## Status

v0.2, per-caller token passthrough over stdio (default) or streamable-HTTP, shared service account only as an
explicit opt-in, read-only. See [SECURITY.md](SECURITY.md) for what's out of scope and why.

## License

MIT, see [LICENSE](LICENSE).
