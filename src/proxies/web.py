from typing import Awaitable, Callable

import httpx
import logfire
import segment.analytics as analytics
from fastapi import Request, Response
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware

from src.container.manager import Container, ContainerManager
from src.settings import settings

HOSTED_LINK_PATHS = ["/link", "/api/auth", "/api/link", "/dpage"]
STATIC_PATHS = ["/__assets", "/__static"]


class WebProxyMiddleware(BaseHTTPMiddleware):
    """
    Proxy web page requests to the mcp-getgather servers.
    - For hosted links, proxy to the server which generated the link.
    - For static pages, proxy to a random unassigned server.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if any(path.startswith(p) for p in HOSTED_LINK_PATHS + STATIC_PATHS) or path == "/":
            try:
                container = await self._get_server_container(path)
            except Exception as e:
                logger.exception(f"Invalid url", error=e, url=path)
                return Response(status_code=400, content="Invalid url")
            return await self._proxy_request(request, container)
        else:
            return await call_next(request)

    async def _proxy_request(self, request: Request, container: Container) -> Response:
        path = request.url.path
        analytics.track(container.hostname, "web_request", {"path": path})  # type: ignore[reportUnknownMemberType]
        logger.info(f"Proxy web request", container=container.dump(), path=path)

        url = f"http://{container.validated_ip}{request.url.path}"
        if request.url.query:
            url += f"?{request.url.query}"

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings.PROXY_TIMEOUT, read=settings.PROXY_READ_TIMEOUT)
        ) as client:
            response = await client.request(
                method=request.method,
                url=url,
                headers={**dict(request.headers), **logfire.get_context()},
                content=await request.body(),
            )

        return Response(
            content=response.content,
            status_code=response.status_code,
            headers=dict(response.headers),
        )

    def _get_hostname_from_link(self, path: str) -> str:
        """All hosted link paths end with a link_id in the format of [HOSTNAME]-[id]."""
        link_id = path.rstrip("/").split("/")[-1]
        parts = link_id.split("-")
        if len(parts) < 2:
            raise ValueError(f"Invalid link id: {link_id}")

        return "-".join(parts[:-1])

    async def _get_server_container(self, path: str) -> Container:
        if any(path.startswith(p) for p in STATIC_PATHS) or path == "/":
            return await ContainerManager.get_unassigned_container()

        # hosted link
        hostname = self._get_hostname_from_link(path)
        return await ContainerManager.get_container_by_hostname(hostname)
