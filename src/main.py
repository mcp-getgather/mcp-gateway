from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI

from src.auth import setup_mcp_auth
from src.hosted_link_proxy import HostedLinkProxyMiddleware
from src.logs import setup_logging
from src.mcp import get_mcp_apps
from src.server_manager import ServerConfig
from src.settings import settings

mcp_apps = get_mcp_apps()


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(settings.LOG_LEVEL)
    ServerConfig.load()
    async with AsyncExitStack() as stack:
        for mcp_app in mcp_apps.values():
            await stack.enter_async_context(mcp_app.lifespan(app))
        yield


app = FastAPI(lifespan=lifespan)
setup_mcp_auth(app, list(mcp_apps.keys()))
for route, mcp_app in mcp_apps.items():
    app.mount(route, mcp_app)

app.add_middleware(HostedLinkProxyMiddleware)


@app.get("/servers")
async def get_servers():
    return ServerConfig.get()


@app.post("/servers")
async def update_servers():
    return ServerConfig.load().get()
