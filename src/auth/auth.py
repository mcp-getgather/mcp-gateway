from typing import cast

from fastapi import FastAPI, HTTPException
from fastapi.requests import Request
from fastapi.templating import Jinja2Templates
from fastmcp.server.dependencies import get_access_token
from loguru import logger
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import TokenVerifier
from pydantic import BaseModel, computed_field
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.responses import RedirectResponse
from starlette.types import Receive, Scope, Send

from src.auth.constants import OAUTH_PROVIDER_NAME
from src.auth.multi_oauth_provider import MultiOAuthProvider, auth_enabled
from src.settings import FRONTEND_DIR, settings


class RequireAuthMiddlewareCustom(RequireAuthMiddleware):
    """
    Custom RequireAuthMiddleware to require authentication for MCP routes.
    If requests are from non mcp clients, redirect to the home page.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        path = scope.get("path")

        if path and path.startswith("/mcp"):
            headers = Headers(scope=scope)
            accept = headers.get("accept") or ""

            if "text/event-stream" not in accept:
                # if client does not accept text/event-stream, redirect to the home page
                response = RedirectResponse(url="/", status_code=307)
                await response(scope, receive, send)
            else:
                await super().__call__(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def setup_mcp_auth(app: FastAPI, mcp_routes: list[str]):
    if not auth_enabled():
        logger.warning("MCP authentication is disabled")
        return

    logger.info("Setting up MCP authentication")

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

    @app.get("/signin")
    def auth_options(  # type: ignore[reportUnusedFunction]
        request: Request, github_url: str | None = None, google_url: str | None = None
    ):
        """Page to allow user to select the authentication provider."""
        if not github_url and not google_url:
            return HTTPException(status_code=500, detail="No authentication providers configured")

        templates = Jinja2Templates(directory=FRONTEND_DIR)
        return templates.TemplateResponse(
            request,
            "auth_options.html",
            context={"google_url": google_url, "github_url": github_url},
        )


class AuthUser(BaseModel):
    sub: str
    auth_provider: OAUTH_PROVIDER_NAME

    name: str | None = None

    # github specific
    login: str | None = None

    # google specific
    email: str | None = None

    # getgather specific
    app_name: str | None = None

    @property
    def user_id(self) -> str:
        """Unique user name combining login and auth provider"""
        return f"{self.sub}.{self.auth_provider}"

    @computed_field
    @property
    def is_admin(self) -> bool:
        return bool(
            self.auth_provider == "google"
            and self.email
            and self.email.lower().endswith(f"@{settings.ADMIN_EMAIL_DOMAIN}")
        )

    @classmethod
    def from_user_id(cls, user_id: str) -> "AuthUser":
        parts = user_id.split(".")
        if len(parts) != 2:
            raise ValueError(f"Invalid user id: {user_id}")
        return cls(sub=".".join(parts[:-1]), auth_provider=cast(OAUTH_PROVIDER_NAME, parts[-1]))

    def dump(self):
        return self.model_dump(exclude_none=True, mode="json")


def get_auth_user() -> AuthUser:
    if not auth_enabled():
        # for testing only when auth is disabled
        return AuthUser(sub="test_user", auth_provider="getgather")

    token = get_access_token()
    if not token:
        raise RuntimeError("No auth user found")

    sub = token.claims.get("sub")
    name = token.claims.get("name")
    login = token.claims.get("login")
    email = token.claims.get("email")
    app_name = token.claims.get("app_name")
    provider = token.claims.get("auth_provider")
    if not sub or not provider:
        raise RuntimeError("Missing sub or provider in auth token")

    return AuthUser(
        sub=sub, auth_provider=provider, name=name, login=login, email=email, app_name=app_name
    )
