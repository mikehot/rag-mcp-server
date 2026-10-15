# Security Note

What this server can do, what it holds, and what is and is not hardened in this version. Verified
against the code in this repo and the installed `mcp` SDK (2.3.0) on 2026-10-15, not asserted from
memory — see "How this was checked" at the end.

## What this server can do

Three read-only tools, proxied unchanged from the Java backend's own tool boundary: `search_knowledge`,
`list_documents`, `get_document_status`. No write, upload, delete, or admin operation is exposed, because
none of those exist in the backend's `AgentToolService` that this server calls — there is nothing here to
disable, the capability was never built.

## The single service-account model (the central tradeoff)

This server logs in once as **one configured service account** (`RAG_MCP_SERVICE_USERNAME` /
`RAG_MCP_SERVICE_PASSWORD`) and uses that account's JWT for every tool call, regardless of who or what is
on the other end of the MCP connection. It does **not** pass through or ask for the end user's own
identity.

**Consequence**: everyone who can talk to this server (every MCP client it's wired into) sees exactly what
that one service account's ACL grants allow — no more, no less. This is a deliberate v1 simplification, not
an oversight; per-caller identity (OAuth / token passthrough to the Java backend) is real future work, not
implemented here.

**What this means operationally**:
- Create a dedicated account for this server, scoped to only the knowledge bases it should expose. Do not
  point `RAG_MCP_SERVICE_USERNAME` at an admin account.
- Anyone who can configure or run this server (e.g. edit their Claude Desktop config) gets that account's
  full read access to those knowledge bases. Treat the credential with the same care as a shared read-only
  API key, not as a per-user login.
- If you need different callers to see different documents, this version cannot do that — don't deploy it
  as a shared service until identity passthrough exists.

## What this server holds, and where

- **The service-account password**: read once from the environment (`RAG_MCP_SERVICE_PASSWORD`) at login
  time, sent only in the login request body over HTTPS/HTTP to the backend, never logged, never written to
  disk. Put it in `.env` locally (gitignored, see `.env.example`) or your MCP client's own secret-config
  mechanism — never in a committed file.
- **The JWT it gets back**: held in memory only (`BackendClient._token`), forwarded as a `Bearer` header on
  every tool call, re-fetched automatically before it expires. It is never written to disk and does not
  survive a process restart.
- **Nothing is persisted by this server.** It has no database and no log file of its own; every tool call
  is stateless from this process's point of view (the Java backend's own `ask_log`/audit tables are a
  separate concern — see rag-knowledge-base's `docs/delivery/DATA_HANDLING.md`, which this server does not
  touch at all, since it never calls `/api/ask`).

## Logging

Checked the installed `mcp` SDK's source directly (`mcp/server/mcpserver/server.py`,
`tools/tool_manager.py`): on a rejected tool call it logs only the tool name and the **names** of the
invalid argument fields, never their values; on a `ToolError` it logs the tool name and the error message
— which, in this server, are the fixed, generic strings in `server.py`'s `_FAILURE_MESSAGES`, never the
raw query text, document content, or credentials. This server's own code (`backend_client.py`) contains no
logging calls of its own. So: no question text, document content, password, or token appears in this
server's logs, by construction, not by configuration you have to remember to set.

## Transport: stdio only, by design, for now

The default and tested transport is `stdio` — this server runs as a local subprocess that your MCP client
(Claude Desktop, Claude Code, etc.) launches and talks to directly; nothing listens on a network port, so
there is no remote attacker surface in this mode.

The underlying SDK also supports `sse` and `streamable-http` transports for remote access, and
`create_server()`'s `MCPServer.run()` could be pointed at either — **this has not been set up, tested, or
hardened here**. Running it that way would expose these tools over the network with no MCP-level auth of
its own (the service account auth is between this server and the Java backend, not between a remote MCP
client and this server). Don't expose this server remotely without adding that layer first; treat
`stdio`-only as the supported mode of this version.

## What is explicitly out of scope for v1

- Per-user identity / OAuth passthrough (see above).
- Rate limiting or its own call budget on top of the Java backend's own (the backend's `AgentToolService`
  still caps a single `/api/agent/tools/execute` request at 3 calls, but this server issues one call per
  MCP tool invocation, so that cap does not meaningfully bound a client that just calls the tool many
  times in a row).
- Remote transport hardening (previous section).
- Credential rotation tooling — rotating the service account's password means updating the env var and
  restarting this server; there's no hot-reload.

## How this was checked

File:line claims above were read from this repo's `src/rag_mcp_server/` and the installed `mcp==2.3.0`
package on 2026-10-15, the same day this server was first written — not carried over from an older
version or another project's docs. Re-check this file against the code if either changes materially.
