from typing import cast

import aiofiles
from fastapi import FastAPI, HTTPException
from fastapi.requests import Request
from fastapi.responses import HTMLResponse
from fastmcp.server.dependencies import get_access_token
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import TokenVerifier
from pydantic import BaseModel
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.types import Receive, Scope, Send

from src.multi_oauth_provider import OAUTH_PROVIDER_TYPE, OAUTH_PROVIDERS, MultiOAuthProvider
from src.settings import PROJECT_DIR


class RequireAuthMiddlewareCustom(RequireAuthMiddleware):
    """Custom RequireAuthMiddleware to require authentication for MCP routes"""

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        path = scope.get("path")
        if path and path.startswith("/mcp"):
            await super().__call__(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def setup_mcp_auth(app: FastAPI, mcp_routes: list[str]):
    auth_provider = MultiOAuthProvider()

    # Set up OAuth routes
    for route in auth_provider.get_routes():
        app.add_route(
            route.path,
            route.endpoint,
            list(route.methods) if route.methods else [],
        )

        # handle '/.well-known/oauth-authorization-server/mcp-*' and
        # '/.well-known/oauth-authorization-server/mcp-*'
        if route.path.startswith("/.well-known"):
            for mcp_route in mcp_routes:
                app.add_route(
                    f"{route.path}{mcp_route}",
                    route.endpoint,
                    list(route.methods) if route.methods else [],
                )

    # Set up OAuth middlewares, in this order:
    auth_middleware = [
        Middleware(
            RequireAuthMiddlewareCustom,  # verify auth for MCP routes
            auth_provider.required_scopes,
        ),
        Middleware(AuthContextMiddleware),  # store the auth user in the context_var
        Middleware(
            AuthenticationMiddleware,  # manage oauth flow
            backend=BearerAuthBackend(cast(TokenVerifier, auth_provider)),
        ),
    ]

    for middleware in auth_middleware:
        app.add_middleware(middleware.cls, *middleware.args, **middleware.kwargs)

    @app.get("/auth_options")
    async def auth_options(request: Request):  # type: ignore[reportUnusedFunction]
        """Page to allow user to select the authentication provider."""
        async with aiofiles.open(PROJECT_DIR / "frontend" / "auth_options.html") as f:
            html = await f.read()

        for provider in OAUTH_PROVIDERS:
            name = f"{provider.upper()}_AUTH_URL"
            url = request.query_params.get(f"{provider}_url")
            if not url:
                return HTTPException(
                    status_code=400, detail=f"Missing {provider}_url in query params"
                )

            html = html.replace(f"INJECTED_{name}", f'window.{name} = "{url}"')

        return HTMLResponse(html)


class AuthUser(BaseModel):
    sub: str
    auth_provider: OAUTH_PROVIDER_TYPE

    name: str | None = None

    # github specific
    login: str | None = None

    # google specific
    email: str | None = None

    @property
    def user_id(self) -> str:
        """Unique user name combining login and auth provider"""
        return f"{self.sub}.{self.auth_provider}"


def get_auth_user() -> AuthUser:
    token = get_access_token()
    if not token:
        raise RuntimeError("No auth user found")

    sub = token.claims.get("sub")
    name = token.claims.get("name")
    login = token.claims.get("login")
    email = token.claims.get("email")
    provider = token.claims.get("auth_provider")
    if not sub or not provider:
        raise RuntimeError("Missing sub or provider in auth token")

    return AuthUser(sub=sub, auth_provider=provider, name=name, login=login, email=email)
