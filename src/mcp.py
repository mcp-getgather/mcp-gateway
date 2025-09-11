from typing import NamedTuple
from urllib.parse import urlparse

from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.proxy import FastMCPProxy, ProxyClient

from src.auth import get_auth_user
from src.logs import logger
from src.session import session_manager
from src.settings import settings

# TODO: move the hosts mapping to a database
# map github [username]@github to mcp host, example:
# MCP_HOSTS = {
#     # "bin-ario@github": "http://127.0.0.1:23456",  # for local
#     "bin-ario@github": "http://host.docker.internal:23456",  # for docker
# }
MCP_HOSTS = {
    f"{name}@github": f"http://mcp-{name}.flycast"
    for name in ["bin-ario", "yuxicreate", "ariya", "kpprasa", "scoutcallens"]
}

MCPRoute = NamedTuple("MCPRoute", [("name", str), ("path", str)])
MCP_ROUTES = [
    MCPRoute("All", "/mcp"),
    MCPRoute("Books", "/mcp-books"),
    MCPRoute("Food", "/mcp-food"),
]


def _create_client_factory(path: str):
    def _create_client():
        user = get_auth_user()
        server_host = MCP_HOSTS[user.login]
        gateway_host = urlparse(settings.SERVER_ORIGIN)

        logger.info(f"Proxy user requests for {user.login} to {server_host}{path}")
        return ProxyClient[StreamableHttpTransport](
            StreamableHttpTransport(
                f"{server_host}{path}",
                headers={
                    "x-forwarded-proto": gateway_host.scheme,
                    "x-forwarded-host": gateway_host.netloc,
                    "x-gateway-session-id": session_manager.create(server_host),
                },
            )
        )

    return _create_client


def _get_mcp_proxy(route: MCPRoute):
    proxy = FastMCPProxy(
        client_factory=_create_client_factory(route.path), name=f"GetGather {route.name} Proxy"
    )
    return proxy.http_app(path="/")


def get_mcp_apps():
    gateways = {route.path: _get_mcp_proxy(route) for route in MCP_ROUTES}
    return gateways
