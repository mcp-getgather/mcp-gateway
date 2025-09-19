from typing import NamedTuple
from urllib.parse import urlparse

from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.proxy import FastMCPProxy, ProxyClient

from src.auth import get_auth_user
from src.logs import logger
from src.server_manager import ServerManager
from src.settings import settings

MCPRoute = NamedTuple("MCPRoute", [("name", str), ("path", str)])
MCP_ROUTES = [
    MCPRoute("All", "/mcp"),
    MCPRoute("Books", "/mcp-books"),
    MCPRoute("Food", "/mcp-food"),
]


def _create_client_factory(path: str):
    def _create_client():
        user = get_auth_user()
        server_host = ServerManager.get_server_from_user(user.login)
        gatewway_origin = urlparse(settings.GATEWAY_ORIGIN)

        logger.info(f"Proxy user requests for {user.login} to {server_host}{path}")
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
    return proxy.http_app(path="/")


def get_mcp_apps():
    proxies = {route.path: _get_mcp_proxy(route) for route in MCP_ROUTES}
    return proxies
