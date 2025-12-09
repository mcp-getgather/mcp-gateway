import time
from typing import cast
from unittest.mock import patch

import httpx
import pytest
from assertpy import assert_that
from fastapi import FastAPI
from mcp.server.auth.provider import AuthorizationCode
from mcp.shared.auth import OAuthToken
from pydantic import AnyUrl
from starlette.routing import Route
from uvicorn import Server

from src import mcp_client
from src.auth.constants import GETGATHER_OAUTH_PROVIDER_NAME, OAUTH_SCOPES
from src.auth.multi_oauth_provider import auth_enabled
from src.auth.third_party_providers import get_provider_scopes
from src.container.container import Container
from src.settings import settings


@pytest.mark.asyncio
async def test_service_startup(server: Server):
    app = cast(FastAPI, server.config.app)
    routes = [cast(Route, route).path for route in app.routes]
    assert_that(routes).contains("/admin/reload", "/mcp", "/mcp-media", "/mcp-books")
    if auth_enabled():
        assert_that(routes).contains(
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-protected-resource",
            "/auth/callback",
            "/authorize",
            "/token",
            "/register",
            "/signin",
            "/account/{mcp_name}",
        )


@pytest.mark.asyncio
async def test_account(server: Server):
    """Test the account page flow with getgather auth provider."""
    user_id = "test_user_id"
    app_key, app_name = list(settings.GETGATHER_APPS.items())[0]
    mcp_name = "mcp-media"
    url = f"{settings.GATEWAY_ORIGIN}/account/{mcp_name}?data_format=json"

    # Initiate auth flow
    async with httpx.AsyncClient() as client:
        response = await client.get(url, follow_redirects=True)

    # Verify redirect to signin page
    assert response.url.path == "/signin"

    # Mock auth token exchange since getgather auth provider does not issue code
    oauth_data = list(mcp_client._oauth_states.values())[0]  # type: ignore[reportPrivateUsage]
    auth_code = AuthorizationCode(
        code="test_code",
        scopes=OAUTH_SCOPES,
        expires_at=time.time() + 1000,
        client_id=oauth_data.client_id or "",
        code_challenge=oauth_data.code_challenge or "",
        redirect_uri=AnyUrl(f"{settings.GATEWAY_ORIGIN}/client/auth/callback"),
        redirect_uri_provided_explicitly=True,
        resource=settings.GATEWAY_ORIGIN,
    )
    oauth_token = OAuthToken(
        access_token=f"{GETGATHER_OAUTH_PROVIDER_NAME}_{app_key}_{user_id}",
        expires_in=1000,
        scope=" ".join(get_provider_scopes()),
    )
    callback_url = f"{auth_code.redirect_uri}?code={auth_code.code}&state={oauth_data.state}"
    with (
        patch(
            "src.auth.multi_oauth_provider.MultiOAuthProvider.load_authorization_code",
            return_value=auth_code,
        ),
        patch(
            "src.auth.multi_oauth_provider.MultiOAuthProvider.exchange_authorization_code",
            return_value=oauth_token,
        ),
    ):
        async with httpx.AsyncClient() as client:
            response = await client.get(callback_url, follow_redirects=True)

    # Verify redirect to back to account page
    assert response.url.path == f"/account/{mcp_name}"

    data = response.json()
    assert data["user"] == {
        "sub": user_id,
        "auth_provider": "getgather",
        "app_name": app_name,
        "is_admin": False,
    }

    container_data = data["container"]
    # Mock extra container data provied by inspect for validation
    container_data.update({"info": {}, "network_name": "internal-net"})
    container = Container.model_validate(container_data)
    assert container.status == "running"
