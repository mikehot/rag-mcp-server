"""Caller-token handling: parsing, validation, and the fail-closed error type.

Identity comes only from a bearer token. This module never decodes the token (the backend is
the only authority on who it belongs to and whether it is still valid) and never logs it.
"""

from __future__ import annotations

import re
from typing import Literal

# RFC 6750 b64token characters. Notably excludes whitespace, quotes, and control characters,
# so a value that passes cannot smuggle a second header line or a "Bearer " prefix.
_TOKEN_RE = re.compile(r"[A-Za-z0-9\-._~+/]+=*")
MAX_TOKEN_LENGTH = 8192

Reason = Literal["missing", "malformed"]


class CredentialError(Exception):
    """The caller supplied no usable token. The message is fixed text, safe to show and log."""

    def __init__(self, reason: Reason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def validate_token(raw: str | None) -> str:
    """Return a clean token, or raise :class:`CredentialError` (never echoing the input)."""
    token = (raw or "").strip()
    if not token:
        raise CredentialError("missing", "No user token was provided.")
    if len(token) > MAX_TOKEN_LENGTH or not _TOKEN_RE.fullmatch(token):
        raise CredentialError(
            "malformed",
            "The user token is malformed: it must be the bare token only, without a 'Bearer ' "
            "prefix, quotes, or whitespace.",
        )
    return token


def token_from_authorization_header(value: str | None) -> str:
    """Extract the token from an ``Authorization: Bearer <token>`` header value."""
    if not value or not value.strip():
        raise CredentialError("missing", "No Authorization header was provided.")
    scheme, _, rest = value.strip().partition(" ")
    if scheme.lower() != "bearer":
        raise CredentialError("malformed", "The Authorization header must use the Bearer scheme.")
    if not rest.strip():
        raise CredentialError("missing", "The Authorization header carries no token.")
    return validate_token(rest)
