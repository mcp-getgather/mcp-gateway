from contextvars import ContextVar
from time import sleep
from typing import Any, NamedTuple
from urllib.parse import urlparse

import httpx
import logfire
import segment.analytics as analytics
from fastmcp import FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.proxy import FastMCPProxy, ProxyClient
from pydantic import BaseModel

from src.auth.auth import get_auth_user
from src.container.manager import ContainerManager
from src.container.service import CONTAINER_STARTUP_SECONDS
from src.logs import logger
from src.proxy_sessions import get_proxy_env_for_hostname, intercept_and_store_proxy_location
from src.settings import settings

# Context variable to store incoming request headers
incoming_headers_context: ContextVar[dict[str, str]] = ContextVar("incoming_headers", default={})

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
            f"Proxy MCP request for {user.user_id} ({user.name}) to {container.hostname} ({container.validated_ip})"
        )
        logger.debug(f"@@@@ MCP request method: {context.method}, message: {data['message']}")

        # Log request metadata if available
        if hasattr(context, "request_meta") and context.request_meta:
            logger.debug(f"@@@@ Request metadata: {context.request_meta}")

        result = await call_next(context)
        logger.debug(f"@@@@ MCP response: {result}")
        return result


def _create_client_factory(path: str):
    async def _create_client():
        logger.info(f"@@@@ CREATING CLIENT FOR PATH: {path}")
        user = get_auth_user()
        container = await ContainerManager.get_user_container(user)
        gatewway_origin = urlparse(settings.GATEWAY_ORIGIN)

        headers = {
            "x-forwarded-proto": gatewway_origin.scheme,
            "x-forwarded-host": gatewway_origin.netloc,
        }
        headers.update(logfire.get_context())

        # Forward x-location header from incoming request if present
        incoming_headers = incoming_headers_context.get()
        if "x-location" in incoming_headers:
            logger.debug(f"@@@@ Intercepted x-location: {incoming_headers['x-location']}")

            # Validate and store proxy location for this hostname session
            proxy_validated = await intercept_and_store_proxy_location(incoming_headers, container.hostname)

            if proxy_validated:
                headers["x-location"] = incoming_headers["x-location"]
                headers["x-proxy-session-id"] = container.hostname  # Only add session ID when proxy is validated
                logger.debug(f"@@@@ Forwarding x-location: {incoming_headers['x-location']}")
                logger.debug(f"@@@@ Forwarding proxy session_id (hostname): {container.hostname}")

                # Get proxy environment config and forward to backend
                proxy_env = get_proxy_env_for_hostname(container.hostname)
                if proxy_env:
                    for key, value in proxy_env.items():
                        headers[f"x-proxy-env-{key.lower()}"] = value
                    logger.debug(f"@@@@ Forwarding proxy env vars: {list(proxy_env.keys())}")
            else:
                logger.warning(f"@@@@ Proxy validation failed, not forwarding proxy headers for {container.hostname}")

        logger.info(
            f"Proxy {path} connection for {user.user_id} ({user.name}) to {container.hostname} ({container.validated_ip})"
        )
        logger.debug(f"@@@@ Proxy headers being sent: {headers}")
        logger.debug(f"Target URL: http://{container.validated_ip}{path}")
        data = user.model_dump(exclude_none=True)
        data["path"] = path
        analytics.identify(container.hostname, data)  # type: ignore[reportUnknownMemberType]

        return ProxyClient[StreamableHttpTransport](
            StreamableHttpTransport(
                f"http://{container.validated_ip}{path}",
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

    url = f"http://{container.validated_ip}/api/docs-mcp"

    async with httpx.AsyncClient() as client:
        response = await client.request(method="GET", url=url)
        routes = [MCPRoute(item["name"], item["route"]) for item in response.json()]

    return routes
