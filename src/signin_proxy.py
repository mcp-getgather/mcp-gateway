from typing import Awaitable, Callable

import httpx
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from src.logs import logger
from src.session import session_manager

TIMEOUT = 30.0
SIGNIN_PATHS = ["/link", "/api"]
STATIC_PATHS = ["/__assets", "/__static"]


class SigninProxyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]):
        path = request.url.path
        if any(path.startswith(p) for p in SIGNIN_PATHS + STATIC_PATHS):
            try:
                downstream_base_url = self._get_downstream_host(path)
            except Exception as e:
                logger.error(f"Invalid url: {path}, error: {e}", exc_info=True)
                return Response(status_code=400, content="Invalid url")
            return await self._proxy_request(request, downstream_base_url)
        else:
            return await call_next(request)

    async def _proxy_request(self, request: Request, downstream_base_url: str) -> Response:
        url = f"{downstream_base_url}{request.url.path}"
        if request.url.query:
            url += f"?{request.url.query}"

        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
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

    def _get_downstream_host(self, path: str) -> str:
        """
        All signin paths end with a generated id in the format of [link_id]_[gateway_session_id].
        We can extract the gateway_session_id and localte the server host from the session manager.
        """
        if any(path.startswith(p) for p in STATIC_PATHS):  
            return session_manager.pick_random()

        code = path.rstrip("/").split("/")[-1]
        _, session_id = code.split("-")
        return session_manager.get(session_id)
