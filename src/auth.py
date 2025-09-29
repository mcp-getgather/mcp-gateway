from typing import cast

from fastapi import FastAPI
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.dependencies import get_access_token
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import TokenVerifier
from pydantic import BaseModel
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.types import Receive, Scope, Send

from src.settings import settings


class RequireAuthMiddlewareCustom(RequireAuthMiddleware):
    """Custom RequireAuthMiddleware to require authentication for MCP routes"""

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        path = scope.get("path")
        if path and path.startswith("/mcp"):
            await super().__call__(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def setup_mcp_auth(app: FastAPI, mcp_routes: list[str]):
    github_auth_provider = GitHubProvider(
        client_id=settings.OAUTH_GITHUB_CLIENT_ID,
        client_secret=settings.OAUTH_GITHUB_CLIENT_SECRET,
        base_url=settings.GATEWAY_ORIGIN,
        redirect_path=settings.OAUTH_GITHUB_REDIRECT_PATH,
        required_scopes=["user"],
    )

    # Set up OAuth routes
    for route in github_auth_provider.get_routes():
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
            github_auth_provider.required_scopes,
        ),
        Middleware(AuthContextMiddleware),  # store the auth user in the context_var
        Middleware(
            AuthenticationMiddleware,  # manage oauth flow
            backend=BearerAuthBackend(cast(TokenVerifier, github_auth_provider)),
        ),
    ]

    for middleware in auth_middleware:
        app.add_middleware(middleware.cls, *middleware.args, **middleware.kwargs)


class AuthUser(BaseModel):
    login: str
    email: str | None = None
    auth_provider: str = settings.auth_provider  # only supports GitHub for now

    @property
    def user_name(self) -> str:
        """Unique user name combining login and auth provider"""
        return f"{self.login}.{self.auth_provider}"


def get_auth_user() -> AuthUser:
    token = get_access_token()
    if not token:
        raise RuntimeError("No auth user found")

    login = token.claims.get("login")
    email = token.claims.get("email")
    if not login:
        raise RuntimeError("No login found in auth token")

    return AuthUser(login=login, email=email)
