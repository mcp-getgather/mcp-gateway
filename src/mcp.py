from time import sleep
from typing import NamedTuple
from urllib.parse import urlparse

import httpx
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.proxy import FastMCPProxy, ProxyClient

from src.auth import get_auth_user
from src.logs import logger
from src.server_manager import CONTAINER_STARTUP_TIME, ServerManager
from src.settings import settings

MCPRoute = NamedTuple("MCPRoute", [("name", str), ("path", str)])


def _create_client_factory(path: str):
    async def _create_client():
        user = get_auth_user()
        server_host = await ServerManager.get_user_hostname(user)
        gatewway_origin = urlparse(settings.GATEWAY_ORIGIN)

        logger.info(f"Proxy mcp requests for {user.user_id} / {user.name} to {server_host}{path}")
        return ProxyClient[StreamableHttpTransport](
            StreamableHttpTransport(
                f"http://{server_host}{path}",
                headers={
                    "x-forwarded-proto": gatewway_origin.scheme,
                    "x-forwarded-host": gatewway_origin.netloc,
                },
                sse_read_timeout=settings.PROXY_READ_TIMEOUT,
            )
        )

    return _create_client


def _get_mcp_proxy(route: MCPRoute):
    proxy = FastMCPProxy(
        client_factory=_create_client_factory(route.path), name=f"GetGather {route.name} Proxy"
    )

    @proxy.tool
    def get_user_info():  # type: ignore[reportUnusedFunction]
        """Get information about the authenticated user."""
        user = get_auth_user()
        return user.model_dump(exclude_none=True)

    return proxy.http_app(path="/")


async def get_mcp_apps():
    routes = await _fetch_mcp_routes()
    proxies = {route.path: _get_mcp_proxy(route) for route in routes}
    return proxies


async def _fetch_mcp_routes():
    logger.info("Fetching MCP routes from mcp-getgather container")
    try:
        host = await ServerManager.get_unassigned_server_host()
    except RuntimeError:
        wait_seconds = CONTAINER_STARTUP_TIME.total_seconds()
        logger.info(f"Waiting for {wait_seconds} seconds for containers to start")
        # note: this is intentionally blocking instead of asyncio.sleep
        sleep(CONTAINER_STARTUP_TIME.total_seconds())

        host = await ServerManager.get_unassigned_server_host()

    url = f"http://{host}/api/docs-mcp"

    async with httpx.AsyncClient() as client:
        response = await client.request(method="GET", url=url)
        routes = [MCPRoute(item["name"], item["route"]) for item in response.json()]

    return routes
