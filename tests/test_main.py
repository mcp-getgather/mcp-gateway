from typing import cast

import pytest
from assertpy import assert_that
from fastapi import FastAPI
from starlette.routing import Route
from uvicorn import Server

from src.settings import settings


@pytest.mark.asyncio
async def test_service_startup(server: Server):
    app = cast(FastAPI, server.config.app)
    routes = [cast(Route, route).path for route in app.routes]
    assert_that(routes).contains("/admin/reload", "/mcp", "/mcp-media", "/mcp-books")
    if settings.auth_enabled:
        assert_that(routes).contains(
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-protected-resource",
            "/auth/callback",
            "/authorize",
            "/token",
            "/register",
            "/signin",
            "/account/{mcp_name}",
        )
