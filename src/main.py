import asyncio
from contextlib import AsyncExitStack, asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles

from src.auth import setup_mcp_auth
from src.hosted_link_proxy import HostedLinkProxyMiddleware
from src.logs import logger, setup_logging
from src.mcp import get_mcp_apps
from src.server_manager import ServerManager
from src.settings import FRONTEND_DIR, settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncExitStack() as stack:
        for mcp_app in app.state.mcp_apps.values():
            await stack.enter_async_context(mcp_app.lifespan(app))
        yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(HostedLinkProxyMiddleware)

app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")


@app.get("/health")
async def health():
    return "Ok"


@app.post("/admin/reload")
async def reload_containers(request: Request):
    token = request.headers.get("x-admin-token")
    if not token or token != settings.ADMIN_API_TOKEN:
        raise HTTPException(status_code=401, detail="Missing or invalid admin token")
    await ServerManager.reload_containers(state="all")


async def main():
    """
    Start mcp-getgather containers, fetch MCP routes,
    then set up the FastAPI server and start it.
    """
    setup_logging(level=settings.LOG_LEVEL, sentry_dsn=settings.GATEWAY_SENTRY_DSN)

    await ServerManager.reload_containers()

    app.state.mcp_apps = await get_mcp_apps()
    setup_mcp_auth(app, list(app.state.mcp_apps.keys()))
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
    await server.serve()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down...")
