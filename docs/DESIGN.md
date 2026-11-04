# Design: per-caller identity passthrough

Status: implemented in v0.2.0. Aligns this repo with the sibling
[rag-agent-python](https://github.com/mikehot/rag-agent-python) (`mcp_client.py`, `main.py`), where
identity comes only from the caller's bearer token.

## Problem

v0.1 logged in as one service account and used its JWT for every call, so every MCP client saw the
same ACL-scoped view and the backend's audit trail could not tell callers apart. rag-agent-python
already forwards each caller's own token; the two repos disagreed on who the backend sees.

## Decision

The backend must always see the real caller. The server holds **no identity of its own** by default.

| Transport | Where the token comes from | Lifetime |
|---|---|---|
| stdio (default) | `RAG_USER_TOKEN` in the environment of *that user's* server process | process |
| streamable-HTTP (optional) | `Authorization: Bearer <token>` on **each** request | one request |
| shared service account (degraded, opt-in) | login with `RAG_MCP_SERVICE_*` | cached, explicit |

stdio has no per-request headers (the client launches one subprocess per user), so the process
environment is the only channel; one process = one user. HTTP serves many users from one process,
so identity is read per request and never stored: no per-session, per-connection or global token
state. The server runs `stateless_http=True` so the SDK keeps no session either.

## Rules

1. **Identity comes only from the bearer token.** Tool signatures have no tenant/user/group
   parameters and handlers build backend arguments explicitly, so extra arguments are *dropped*
   (the SDK ignores unknown fields; we do not forward them). Tested with tenant/user/group/token
   look-alikes. We chose "ignore" over "reject" because the SDK offers no reject hook without
   replacing its validation; both satisfy "identity cannot be influenced".
2. **Fail closed.** Missing, malformed (not RFC 6750 b64token charset, whitespace, `Bearer `
   prefix, >8 KiB) or backend-rejected (401/403) token → tool error, and **no request is sent**
   (missing/malformed) or exactly one request with the caller's token (rejected). No retry, no
   re-login, no fallback.
3. **Token is never decoded locally.** The backend is the sole authority on validity and expiry.
   A local `exp` pre-check would add little and could disagree with the backend.
4. **HTTP mode ignores `RAG_USER_TOKEN`** (warns at startup). Otherwise one operator token would
   silently become every remote caller's identity.
5. **No cross-caller state in the HTTP client.** Tokens are per-call arguments, never attributes.
   The shared `httpx` client has cookies disabled via a reject-all policy, because a backend
   `Set-Cookie` on caller A's response would otherwise be replayed on caller B's request.
6. **Logs and errors carry no token, response body, or user name.** Exceptions contain fixed text
   plus an HTTP status code; transport errors are re-raised `from None` because the chained httpx
   request holds the `Authorization` header. The pre-existing `_safe_body` (first 300 chars of the
   response) was removed. httpx's INFO request log is silenced in `main()`.

## Degraded mode (shared service account)

`RAG_MCP_ALLOW_SHARED_SERVICE_ACCOUNT=true` + `RAG_MCP_SERVICE_USERNAME/PASSWORD`. Off by default.

Conservative choices:
- It applies **only when a request carries no token at all.** A token that is present but
  malformed, expired or rejected is *never* masked by the shared account: that would turn an
  expiry into a silent privilege/identity change and hide it from the audit trail.
- Startup warning when enabled; a warning on every call served this way; neither includes
  credentials.
- Credentials set **without** the enable flag make startup fail. A leftover v0.1 config therefore
  cannot be mistaken for an active, safe one; the operator must choose (migration section in the
  README).
- `BackendClient` independently refuses `token=None` when the flag is off (defense in depth against
  a future server-layer bug).
- Cost: callers share an identity and an ACL; the backend audit cannot distinguish them. Use
  only for local single-user trials.

## Trade-offs and open choices

- **Start vs. crash without a token (stdio).** We start and refuse each call with an actionable
  message. Claude Desktop surfaces a crashed server only as "disconnected", which is much harder
  to debug. Nothing is sent to the backend either way.
- **Token expiry in stdio** needs the user to update `RAG_USER_TOKEN` and restart the client; there
  is no refresh flow here (the backend login/refresh contract is not part of this repo's scope).
- **HTTP mode is not a hardened remote service.** The server forwards whatever bearer it gets and
  lets the backend decide; it does no MCP-level auth, rate limiting, or TLS. Default bind is
  loopback; a non-loopback bind logs a warning. Put it behind a TLS-terminating proxy. Stateless
  mode means no server-side session to hijack but also no resumability.
- **403 is treated like 401** (rejected token), matching the requirement. A backend that uses 403
  for "authenticated but not allowed on this endpoint" would be reported as a token problem; the
  message says "expired, revoked, or invalid", and per-resource denials still arrive as
  `PERMISSION_DENIED` results, reported separately.
- **No tenant concept in this repo.** "tenant" is handled as just another ignored argument; tenant
  scoping is the backend's job, derived from the token.

## Test strategy

All with `respx`, no real backend: per-caller Authorization headers (stdio, HTTP, interleaved),
20×2 concurrent HTTP callers with skewed latency, missing/malformed/401/403 with zero service-account
requests, identity-argument injection, cookie non-replay, degraded mode off/on/warning/no-masking,
and log-capture assertions (caplog at DEBUG on the root logger, mutation-checked) for token,
response body and user name.

## Not verified here

Real Claude Desktop, a real backend, and real JWT behaviour (expiry, 401 vs 403 semantics). See the
README's manual verification steps.
