from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from src.auth import setup_mcp_auth
from src.hosted_link_proxy import HostedLinkProxyMiddleware
from src.logs import setup_logging
from src.mcp import get_mcp_apps
from src.server_manager import ServerManager
from src.settings import settings

mcp_apps = get_mcp_apps()


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(level=settings.LOG_LEVEL, sentry_dsn=settings.GATEWAY_SENTRY_DSN)
    await ServerManager.reload_containers()

    async with AsyncExitStack() as stack:
        for mcp_app in mcp_apps.values():
            await stack.enter_async_context(mcp_app.lifespan(app))
        yield


app = FastAPI(lifespan=lifespan)
setup_mcp_auth(app, list(mcp_apps.keys()))
for route, mcp_app in mcp_apps.items():
    app.mount(route, mcp_app)

app.add_middleware(HostedLinkProxyMiddleware)


@app.get("/health")
async def health():
    return "Ok"


@app.post("/admin/reload")
async def reload_containers(request: Request):
    token = request.headers.get("x-admin-token")
    if not token or token != settings.ADMIN_API_TOKEN:
        raise HTTPException(status_code=401, detail="Missing or invalid admin token")
    await ServerManager.reload_containers(force=True)
