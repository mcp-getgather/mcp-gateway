import asyncio
import signal
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from datetime import datetime
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

import logfire
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger

from src.auth.auth import setup_mcp_auth
from src.container.manager import ContainerManager
from src.container.service import ContainerService
from src.http_utils import incoming_headers_context
from src.mcp_client import MCPAuthResponse, auth_and_connect, handle_auth_code
from src.proxies.mcp import get_mcp_apps
from src.proxies.web import WebProxyMiddleware
from src.settings import FRONTEND_DIR, settings


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


def create_app():
    app = FastAPI(lifespan=lifespan)
    logfire.instrument_fastapi(app, capture_headers=True, excluded_urls="/health")

    # Middleware to store incoming request headers for MCP routes
    @app.middleware("http")
    async def store_mcp_headers(  # type: ignore
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:  # type: ignore[reportUnusedFunction]
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
        await ContainerManager.recreate_all_containers()

    @app.get("/account/{mcp_name}")
    async def account(  # type: ignore[reportUnusedFunction]
        request: Request, mcp_name: str, state: str | None = None, data_format: str = "html"
    ):
        result = await auth_and_connect(mcp_name, state, data_format=data_format)
        if isinstance(result, MCPAuthResponse):
            return RedirectResponse(url=result.auth_url)
        else:
            if data_format == "json":
                return JSONResponse(content=result.model_dump(exclude_none=True, mode="json"))

            def to_pacific_time(dt: datetime, format: str = "%Y/%m/%d %H:%M:%S") -> str:
                return dt.astimezone(ZoneInfo("America/Los_Angeles")).strftime(format)

            templates = Jinja2Templates(directory=FRONTEND_DIR)
            templates.env.filters["datetime"] = to_pacific_time  # type: ignore[reportUnknownMemberType]
            return templates.TemplateResponse(
                request,
                "account.html",
                context={
                    "auth_user": result.user,
                    "container": result.container,
                    "manager_info": result.manager_info,
                },
            )

    @app.get("/client/auth/callback")
    async def client_auth_callback(code: str, state: str):  # type: ignore[reportUnusedFunction]
        oauth_data = await handle_auth_code(state=state, code=code)
        url = f"/account/{oauth_data.mcp_name}?state={oauth_data.state}"
        if oauth_data.data_format == "json":
            url += "&data_format=json"
        return RedirectResponse(url=url)

    return app


class NoSignalServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):  # prevent uvicorn from owning global handlers
        yield


async def create_servers():
    """
    Start mcp-getgather containers, fetch MCP routes,
    then set up the FastAPI server and start it.
    """
    await ContainerManager.init_active_assigned_pool()
    await ContainerManager.refresh_standby_pool()

    servers: dict[str, uvicorn.Server] = {}
    for server_config in settings.SERVER_CONFIGS:
        app = create_app()
        app.state.mcp_apps = await get_mcp_apps()
        setup_mcp_auth(app, server_config.origin, list(app.state.mcp_apps.keys()))

        for route, mcp_app in app.state.mcp_apps.items():
            app.mount(route, mcp_app)

        config = uvicorn.Config(
            app=app,
            host="0.0.0.0",
            port=server_config.port,
            log_level=settings.LOG_LEVEL.lower(),
            proxy_headers=True,
            forwarded_allow_ips="*",
            reload=False,  # reload is handled by nodemon since app needs dynamic set up
        )
        servers[server_config.origin] = NoSignalServer(config)
    return servers


async def main():
    servers = await create_servers()
    tasks = [asyncio.create_task(s.serve()) for s in servers.values()]

    # manually handle signals so we can shutdown multiple servers gracefully
    def stop():
        logger.info("Shutting down servers")
        for s in servers.values():
            s.should_exit = True

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, stop)
    loop.add_signal_handler(signal.SIGTERM, stop)

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
