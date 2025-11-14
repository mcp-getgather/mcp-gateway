from time import sleep
from typing import Any, NamedTuple
from urllib.parse import urlparse

import httpx
import logfire
import segment.analytics as analytics
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.proxy import FastMCPProxy, ProxyClient
from pydantic import BaseModel

from src.auth.auth import get_auth_user
from src.container.manager import CONTAINER_STARTUP_SECONDS, ContainerManager
from src.logs import logger
from src.settings import settings

MCPRoute = NamedTuple("MCPRoute", [("name", str), ("path", str)])


class SegmentMiddleware(Middleware):
    async def __call__(self, context: MiddlewareContext, call_next: CallNext[Any, Any]):
        user = get_auth_user()
        container = await ContainerManager.get_user_container(user)

        data: dict[str, Any] = {"method": context.method}
        if isinstance(context.message, BaseModel):
            data["message"] = context.message.model_dump(exclude_none=True)
        else:
            data["message"] = str(context.message)
        analytics.track(container.hostname, "mcp_request", data)  # type: ignore[reportUnknownMemberType]
        logger.info(
            f"Proxy MCP request for {user.user_id} ({user.name}) to {container.hostname} ({container.ip})"
        )

        return await call_next(context)


def _create_client_factory(path: str):
    async def _create_client():
        user = get_auth_user()
        container = await ContainerManager.get_user_container(user)
        gatewway_origin = urlparse(settings.GATEWAY_ORIGIN)

        headers = {
            "x-forwarded-proto": gatewway_origin.scheme,
            "x-forwarded-host": gatewway_origin.netloc,
        }
        headers.update(logfire.get_context())

        logger.info(
            f"Proxy {path} connection for {user.user_id} ({user.name}) to {container.hostname} ({container.ip})"
        )
        data = user.model_dump(exclude_none=True)
        data["path"] = path
        analytics.identify(container.hostname, data)  # type: ignore[reportUnknownMemberType]

        return ProxyClient[StreamableHttpTransport](
            StreamableHttpTransport(
                f"http://{container.ip}{path}",
                headers=headers,
                sse_read_timeout=settings.PROXY_READ_TIMEOUT,
            )
        )

    return _create_client


def _get_mcp_proxy(route: MCPRoute):
    proxy = FastMCPProxy(
        client_factory=_create_client_factory(route.path),
        name=f"GetGather {route.name} Proxy",
        middleware=[SegmentMiddleware()],
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
        container = await ContainerManager.get_unassigned_container()
    except RuntimeError:
        logger.info(f"Waiting for {CONTAINER_STARTUP_SECONDS} seconds for containers to start")
        # note: this is intentionally blocking instead of asyncio.sleep
        sleep(CONTAINER_STARTUP_SECONDS)

        container = await ContainerManager.get_unassigned_container()

    url = f"http://{container.ip}/api/docs-mcp"

    async with httpx.AsyncClient() as client:
        response = await client.request(method="GET", url=url)
        routes = [MCPRoute(item["name"], item["route"]) for item in response.json()]

    return routes
