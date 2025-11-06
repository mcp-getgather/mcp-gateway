import asyncio
from typing import cast

import pytest
from assertpy import assert_that
from starlette.routing import Route

from src.main import app, create_server
from src.server_manager import ServerManager
from src.settings import settings


@pytest.mark.asyncio
async def test_service_startup():
    server = await create_server()
    server_task = asyncio.create_task(server.serve())

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

    containers = await ServerManager._get_containers()  # type: ignore[reportPrivateUsage]
    assert len(containers) == settings.MIN_CONTAINER_POOL_SIZE

    server.should_exit = True
    await server_task
