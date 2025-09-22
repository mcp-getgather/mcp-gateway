from typing import Awaitable, Callable

import httpx
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from src.logs import logger
from src.server_manager import ServerManager
from src.settings import settings

HOSTED_LINK_PATHS = ["/link", "/api/auth", "/api/link", "/dpage"]
STATIC_PATHS = ["/__assets", "/__static"]


class HostedLinkProxyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]):
        path = request.url.path
        if any(path.startswith(p) for p in HOSTED_LINK_PATHS + STATIC_PATHS):
            try:
                server_host = self._get_server_host(path)
            except Exception as e:
                logger.error(f"Invalid url: {path}, error: {e}", exc_info=True)
                return Response(status_code=400, content="Invalid url")
            return await self._proxy_request(request, server_host)
        else:
            return await call_next(request)

    async def _proxy_request(self, request: Request, server_host: str) -> Response:
        url = f"http://{server_host}{request.url.path}"
        if request.url.query:
            url += f"?{request.url.query}"

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings.PROXY_TIMEOUT, read=settings.PROXY_READ_TIMEOUT)
        ) as client:
            response = await client.request(
                method=request.method,
                url=url,
                headers=dict(request.headers),
                content=await request.body(),
            )

        return Response(
            content=response.content,
            status_code=response.status_code,
            headers=dict(response.headers),
        )

    def _get_server_host(self, path: str) -> str:
        """All hosted link paths end with a link_id in the format of [server_name]-[id]."""
        if any(path.startswith(p) for p in STATIC_PATHS):
            return ServerManager.get_random_server()

        link_id = path.rstrip("/").split("/")[-1]
        parts = link_id.split("-")
        if len(parts) < 2:
            raise ValueError(f"Invalid link id: {link_id}")

        host_name = "-".join(parts[:-1])
        server_host = ServerManager.get_server_from_name(host_name)
        return server_host
