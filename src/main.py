import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime
from typing import Awaitable, Callable

import logfire
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from src.auth.auth import setup_mcp_auth
from src.container.manager import ContainerManager
from src.container.service import ContainerService
from src.logs import setup_logging
from src.mcp_client import MCPAuthResponse, auth_and_connect, handle_auth_code
from src.proxies.mcp import get_mcp_apps, incoming_headers_context
from src.proxies.web import WebProxyMiddleware
from src.settings import FRONTEND_DIR, settings

setup_logging(
    level=settings.LOG_LEVEL,
    logs_dir=settings.logs_dir,
    sentry_dsn=settings.GATEWAY_SENTRY_DSN,
    segment_write_key=settings.SEGMENT_WRITE_KEY,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop_event = asyncio.Event()

    async def _maintenance_loop():
        while not stop_event.is_set():
            interval = await ContainerManager.perform_maintenance()
            await asyncio.sleep(interval)

    background_task = asyncio.create_task(_maintenance_loop())

    async with AsyncExitStack() as stack:
        for mcp_app in app.state.mcp_apps.values():
            await stack.enter_async_context(mcp_app.lifespan(app))
        yield

        stop_event.set()
        await background_task


logfire.configure(
    service_name="mcp-gateway",
    send_to_logfire="if-token-present",
    token=settings.LOGFIRE_TOKEN or None,
    environment=settings.ENVIRONMENT,
    code_source=logfire.CodeSource(
        repository="https://github.com/mcp-getgather/mcp-gateway", revision="main"
    ),
)


def create_app():
    app = FastAPI(lifespan=lifespan)
    logfire.instrument_fastapi(app)

    # Middleware to store incoming request headers for MCP routes
    @app.middleware("http")
    async def store_mcp_headers(  # type: ignore
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:  # type: ignore[reportUnusedFunction]
        if request.url.path.startswith("/mcp"):  # type: ignore
            # Store all incoming headers in context for access during request
            incoming_headers_context.set(dict(request.headers))
        response = await call_next(request)
        return response

    app.add_middleware(WebProxyMiddleware)

    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")

    @app.get("/health")
    async def health():  # type: ignore[reportUnusedFunction]
        return f"OK {int(datetime.now().timestamp())} GIT_REV: {settings.GIT_REV}"

    @app.post("/admin/reload")
    async def reload_containers(request: Request):  # type: ignore[reportUnusedFunction]
        token = request.headers.get("x-admin-token")
        if not token or token != settings.ADMIN_API_TOKEN:
            raise HTTPException(status_code=401, detail="Missing or invalid admin token")

        await ContainerService.pull_container_image()
        await ContainerManager.update_containers()

    @app.get("/account/{mcp_name}")
    async def account(mcp_name: str, state: str | None = None):  # type: ignore[reportUnusedFunction]
        result = await auth_and_connect(mcp_name, state)
        if isinstance(result, MCPAuthResponse):
            return RedirectResponse(url=result.auth_url)
        else:
            # TODO: return a web pageinstead of json
            return JSONResponse(
                status_code=200, content=result.model_dump(exclude_none=True, mode="json")
            )

    @app.get("/client/auth/callback")
    async def client_auth_callback(code: str, state: str):  # type: ignore[reportUnusedFunction]
        oauth_data = await handle_auth_code(state=state, code=code)
        return RedirectResponse(url=f"/account/{oauth_data.mcp_name}?state={oauth_data.state}")

    return app


async def create_server():
    """
    Start mcp-getgather containers, fetch MCP routes,
    then set up the FastAPI server and start it.
    """
    await ContainerManager.refresh_standby_pool()

    app = create_app()
    app.state.mcp_apps = await get_mcp_apps()
    if settings.auth_enabled:
        logger.info("Setting up MCP authentication")
        setup_mcp_auth(app, list(app.state.mcp_apps.keys()))
    else:
        logger.warning("MCP authentication is disabled")

    for route, mcp_app in app.state.mcp_apps.items():
        app.mount(route, mcp_app)

    config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=9000,
        log_level=settings.LOG_LEVEL.lower(),
        proxy_headers=True,
        forwarded_allow_ips="*",
        reload=False,  # reload is handled by nodemon since app needs dynamic set up
    )
    server = uvicorn.Server(config)
    return server


async def main():
    server = await create_server()
    await server.serve()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down...")
