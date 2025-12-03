import json
import os
from contextvars import ContextVar
from time import sleep
from typing import Any, NamedTuple
from urllib.parse import urlparse

import aiofiles
import httpx
import logfire
import segment.analytics as analytics
import yaml
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.proxy import FastMCPProxy, ProxyClient
from loguru import logger
from pydantic import BaseModel

from src.auth.auth import get_auth_user
from src.container.container import Container
from src.container.manager import ContainerManager
from src.container.service import CONTAINER_STARTUP_SECONDS
from src.logs import log_decorator
from src.residential_proxy_sessions import select_and_build_proxy_config
from src.settings import settings

incoming_headers_context: ContextVar[dict[str, str]] = ContextVar("incoming_headers", default={})

MCPRoute = NamedTuple("MCPRoute", [("name", str), ("path", str)])


class SegmentMiddleware(Middleware):
    @log_decorator
    async def __call__(self, context: MiddlewareContext, call_next: CallNext[Any, Any]):  # type: ignore[override]
        user = get_auth_user()
        container = await ContainerManager.get_user_container(user)

        data: dict[str, Any] = {"method": context.method}
        if isinstance(context.message, BaseModel):
            data["message"] = context.message.model_dump(exclude_none=True)
        else:
            data["message"] = str(context.message)
        analytics.track(container.hostname, "mcp_request", data)  # type: ignore[reportUnknownMemberType]
        logger.info(f"Proxy MCP request for user", container=container.dump(), user=user.dump())

        return await call_next(context)


async def _write_proxy_config_to_container(
    container_hostname: str,
    proxy_config: dict[str, Any] | None,
) -> None:
    """Write proxy configuration to container's mount directory as proxies.yaml."""

    mount_dir = Container.mount_dir_for_hostname(container_hostname)
    proxies_file = mount_dir / "proxies.yaml"
    logger.debug(
        f"Writing proxy config to container mount",
        hostname=container_hostname,
        file=str(proxies_file),
        proxy_config=proxy_config,
    )

    if proxy_config:
        yaml_content = yaml.dump(proxy_config)
        async with aiofiles.open(proxies_file, "w") as f:
            await f.write(yaml_content)

        # Set file permissions to be readable by container (0o644 = rw-r--r--)
        os.chmod(proxies_file, 0o644)

        logger.info(
            f"Wrote proxy config to container mount",
            hostname=container_hostname,
            file=str(proxies_file),
        )
    else:
        # Remove proxies.yaml if no proxy config
        if proxies_file.exists():
            proxies_file.unlink()
            logger.info(
                f"Removed proxy config from container mount",
                hostname=container_hostname,
                file=str(proxies_file),
            )


def _create_client_factory(path: str):
    @log_decorator
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
            f"Proxy MCP connection for user",
            container=container.dump(),
            user=user.dump(),
            path=path,
        )
        incoming_headers = incoming_headers_context.get()
        headers_for_logging = headers.copy()

        # Extract location and proxy type from headers
        location_data = None
        proxy_name = None

        # Forward all custom x- headers to the container (e.g., x-location, x-signin-id, x-incognito)
        for header_name, header_value in incoming_headers.items():
            if header_name.lower().startswith("x-"):
                headers[header_name] = header_value

                # Special handling for x-location-info to parse JSON
                if header_name.lower() == "x-location-info":
                    try:
                        location_data = json.loads(header_value)
                        logger.debug("Intercepted x-location-info", location=location_data)
                        headers_for_logging[header_name] = location_data
                    except (json.JSONDecodeError, TypeError):
                        logger.debug("Intercepted x-location-info (raw)", location_raw=header_value)
                        headers_for_logging[header_name] = header_value
                # Extract x-proxy-type for proxy selection
                elif header_name.lower() == "x-proxy-type":
                    proxy_name = header_value
                    logger.debug("Intercepted x-proxy-type", proxy_name=proxy_name)
                    headers_for_logging[header_name] = header_value
                # Keep backward compatibility with x-location
                elif header_name.lower() == "x-location":
                    try:
                        location_data = json.loads(header_value)
                        logger.debug("Intercepted x-location", location=location_data)
                        headers_for_logging[header_name] = location_data
                    except (json.JSONDecodeError, TypeError):
                        logger.debug("Intercepted x-location (raw)", location_raw=header_value)
                        headers_for_logging[header_name] = header_value
                else:
                    headers_for_logging[header_name] = header_value

        # Select and build proxy configuration
        print("@@@ Current Settings.PROXIES_CONFIG:", settings.PROXIES_CONFIG)
        if settings.PROXIES_CONFIG:
            print("@@@ Selected proxy config:")
            proxy_config = select_and_build_proxy_config(
                toml_config=settings.PROXIES_CONFIG,
                proxy_name=proxy_name,
                default_proxy_name=settings.DEFAULT_PROXY_TYPE,
                profile_id=container.hostname,
                location=location_data,
            )
            # Write selected proxy config to container mount
            await _write_proxy_config_to_container(container.hostname, proxy_config)

        logger.info("Current Headers", headers=headers_for_logging)
        data = user.dump()
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
        return user.dump()

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
