# Security Note

What this server can do, what it holds, and what is and is not hardened in this version (v0.2.0).
Design rationale and trade-offs: [docs/DESIGN.md](docs/DESIGN.md).

## What this server can do

Three read-only tools, proxied unchanged from the Java backend's own tool boundary: `search_knowledge`,
`list_documents`, `get_document_status`. No write, upload, delete, or admin operation is exposed.

## Authentication model: the caller is the identity

The backend must always see the real caller. This server has no identity of its own by default.

- **stdio (default).** The user's own backend bearer token is supplied through `RAG_USER_TOKEN` in the
  environment of the server process their MCP client launches. One process = one user.
- **streamable-HTTP (optional, `RAG_MCP_TRANSPORT=streamable-http`).** The token is read from the
  `Authorization: Bearer` header of **each request** and forwarded for that request only. Nothing is
  cached per session or per connection (`stateless_http`). `RAG_USER_TOKEN` is ignored in this mode.
- **Identity comes only from the token.** The tools have no tenant/user/group parameters; extra
  arguments a caller sends (`tenant_id`, `user_id`, `group`, `token`, ...) are dropped and never
  forwarded. The backend derives identity and ACL from the token.
- **Fail closed.** A missing or malformed token (wrong characters, whitespace, `Bearer ` prefix,
  over 8 KiB) is refused with a tool error and **no backend request is made**. A token the backend
  rejects (HTTP 401/403) produces a tool error after one request; there is no retry, re-login or
  fallback. The server never decodes the token; the backend decides validity and expiry.

### Degraded mode: shared service account (off by default)

`RAG_MCP_ALLOW_SHARED_SERVICE_ACCOUNT=true` together with `RAG_MCP_SERVICE_USERNAME` /
`RAG_MCP_SERVICE_PASSWORD` serves requests that carry **no token** as one shared service account.

Security meaning: every such caller shares a single identity and ACL; the backend's audit log cannot
tell them apart; anyone who can reach the server (or edit the client config) gets that account's full
read access. Use only for local, single-user trials with a dedicated, minimally-scoped read-only
account (never an admin). Safeguards: disabled by default; setting the credentials without the flag
makes startup fail; it never replaces a token that was supplied but is malformed, expired or rejected;
a warning is logged at startup and on every call served this way (no credentials in it).

## Threat model

| Threat | Mitigation | Residual risk |
|---|---|---|
| Caller A obtains B's data via shared state (HTTP) | Token is a per-call argument; no per-session state; cookies disabled on the shared HTTP client; tested with interleaved and concurrent callers | A bug in future code; the tests are the guard |
| Caller picks another tenant/user/group via tool arguments | No such parameters; extra arguments dropped; identity from token only | `knowledge_base_id` is still caller-chosen, but the backend ACL-filters it for that token |
| Silent privilege change when a token expires | No fallback on rejected/malformed tokens; degraded mode only when no token at all, and off by default | Operator enables degraded mode knowingly |
| Token leaks via logs/errors | Fixed-text errors; `from None` for transport errors; no response bodies; httpx request log silenced; log-capture tests | Third-party log config the operator adds (e.g. DEBUG HTTP wire logging) |
| Header injection through the token | Token charset allow-list; whitespace/CR/LF rejected | None known |
| Token theft on the wire (HTTP mode) | None here: the server does no TLS | Run behind a TLS proxy; bind to loopback (default) |
| Token theft at rest | Token lives in process memory / the client's config `env` block, never written by this server | The client config file holds it in plaintext; protect it like a password |
| Unauthenticated abuse of HTTP mode | Without a token every call is refused and nothing reaches the backend | Not rate limited; reachable port can be probed; no MCP-level auth |
| Stolen/long-lived token | Backend's expiry/revocation applies (401/403 surfaces as an error) | Short-lived tokens are the operator's responsibility |

## Logging boundary

Never logged or placed in errors: bearer tokens, service-account passwords, request headers,
backend response bodies, document text, snippets, user names, query text. What is logged: tool
name and event type (e.g. "credential missing", "backend rejected the caller's token"), startup
and per-call degraded-mode warnings, and the MCP SDK's own messages (tool name and generic error
text only). Exceptions raised in this package carry fixed text and an HTTP status code. This is
enforced by tests that capture all log output at DEBUG across success and failure paths and assert
none of the token, response body or user name appears.

The operator can still defeat this by enabling third-party wire-level logging (e.g. httpx/httpcore
DEBUG, a reverse proxy logging `Authorization`). Don't.

## What this server holds, and where

- **Caller tokens:** stdio holds one in the process environment for the process lifetime; HTTP holds
  one per in-flight request only. Never written to disk, never logged.
- **Service-account password and JWT (degraded mode only):** read from env, sent only in the login
  body; the JWT is cached in memory and re-fetched before expiry.
- **Nothing else is persisted.** No database, no log file of its own.

## Out of scope

- Token refresh / OAuth flows (stdio users replace an expired token and restart the client).
- MCP-level auth, TLS, rate limiting and abuse controls for the HTTP transport.
- Per-user audit inside this server (the backend's audit sees the real caller).
- Real-backend verification of 401-vs-403 semantics and JWT expiry (tested with mocks only).
