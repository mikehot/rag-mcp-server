"""Configuration for the MCP server, loaded from environment variables.

Identity model (see docs/DESIGN.md and SECURITY.md): the backend must always see the real
caller. In stdio mode the caller's token comes from ``RAG_USER_TOKEN``; in streamable-HTTP mode it
comes from each request's ``Authorization`` header. A shared service account exists only as an
explicitly enabled, off-by-default fallback.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Transport = Literal["stdio", "streamable-http"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RAG_MCP_", env_file=".env", extra="ignore", populate_by_name=True
    )

    backend_base_url: str = Field(
        default="http://localhost:8080",
        description="Base URL of the rag-knowledge-base Java backend (no trailing slash).",
    )
    transport: Transport = Field(default="stdio", description="stdio, or streamable-http.")

    # --- per-caller identity -------------------------------------------------------------
    user_token: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("RAG_USER_TOKEN", "user_token"),
        description="stdio mode only: this user's own backend bearer token (bare, no 'Bearer ').",
    )

    # --- explicit, off-by-default degraded mode ------------------------------------------
    allow_shared_service_account: bool = Field(
        default=False,
        description="DEGRADED MODE. When true, a request that carries NO token is served as the "
        "shared service account below instead of being rejected. Every such caller shares one "
        "identity. Never used when a token was supplied but is malformed, expired or rejected.",
    )
    service_username: str | None = None
    service_password: SecretStr | None = None

    # --- streamable-HTTP listener --------------------------------------------------------
    http_host: str = "127.0.0.1"
    http_port: int = Field(default=8000, ge=1, le=65535)

    request_timeout_seconds: float = Field(default=30.0, ge=1.0)
    token_refresh_margin_seconds: float = Field(
        default=60.0,
        ge=0.0,
        description="Service-account fallback only: re-login this many seconds before the "
        "cached token's reported expiry.",
    )

    @model_validator(mode="after")
    def _service_account_only_when_enabled(self) -> Settings:
        if self.allow_shared_service_account:
            if not self.service_username or self.service_password is None:
                raise ValueError(
                    "RAG_MCP_ALLOW_SHARED_SERVICE_ACCOUNT=true requires RAG_MCP_SERVICE_USERNAME "
                    "and RAG_MCP_SERVICE_PASSWORD"
                )
        elif self.service_username or self.service_password is not None:
            # Fail closed on a stale config: credentials that would silently do nothing (or,
            # worse, be assumed active) are rejected rather than ignored.
            raise ValueError(
                "Service-account credentials are set but RAG_MCP_ALLOW_SHARED_SERVICE_ACCOUNT is "
                "not enabled. Remove them (recommended) or enable the degraded mode explicitly."
            )
        return self


def load_settings() -> Settings:
    return Settings()
