from __future__ import annotations

import pytest

from rag_mcp_server.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        backend_base_url="http://backend.test",
        service_username="svc-mcp",
        service_password="not-a-real-password",
        token_refresh_margin_seconds=60.0,
    )
