"""Configuration for the MCP server, loaded from environment variables.

Every value here is either a connection detail (where the Java backend is) or the
service-account credential this server logs in as. See SECURITY.md for what that
account can see and how its token is handled.
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RAG_MCP_", env_file=".env", extra="ignore")

    backend_base_url: str = Field(
        default="http://localhost:8080",
        description="Base URL of the rag-knowledge-base Java backend (no trailing slash).",
    )
    service_username: str = Field(
        description="Username of the dedicated service account this server logs in as. "
        "See SECURITY.md: this account's own ACL grants bound everything this server can read."
    )
    service_password: SecretStr = Field(
        description="Password for the service account. Set via env var, never hard-code or log it."
    )
    request_timeout_seconds: float = Field(default=30.0, ge=1.0)
    token_refresh_margin_seconds: float = Field(
        default=60.0,
        ge=0.0,
        description="Re-login this many seconds before the cached token's reported expiry, "
        "so a call never starts on a token that expires mid-request.",
    )


def load_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # required fields come from the environment
