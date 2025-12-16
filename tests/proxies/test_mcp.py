import pytest
from fastmcp import Client

from src.auth.auth import AuthUser
from src.auth.constants import GETGATHER_OAUTH_PROVIDER_NAME
from src.settings import settings
from tests.conftest import ServerWithOrigin


@pytest.mark.asyncio
async def test_mcp_getgather_auth(server: ServerWithOrigin):
    user_id = "test_user_id"
    app_key, app_name = list(settings.GETGATHER_APPS.items())[0]
    url = f"{server.origin}/mcp-media"
    token = f"{GETGATHER_OAUTH_PROVIDER_NAME}_{app_key}_{user_id}"
    async with Client(url, auth=token) as client:
        result = await client.call_tool_mcp("get_user_info", {})

    assert (
        result.structuredContent
        == AuthUser(sub=user_id, auth_provider="getgather", app_name=app_name).dump()
    )


@pytest.mark.asyncio
async def test_mcp_github_auth(server: ServerWithOrigin):
    url = f"{server.origin}/mcp-media"
    token = settings.TEST_GITHUB_OAUTH_TOKEN
    async with Client(url, auth=token) as client:
        result = await client.call_tool_mcp("get_user_info", {})

    assert not result.isError
    user = AuthUser.model_validate(result.structuredContent)
    assert user.auth_provider == "github"


@pytest.mark.asyncio
async def test_npr(server: ServerWithOrigin):
    user_id = "test_user_id"
    app_key = list(settings.GETGATHER_APPS.keys())[0]
    url = f"{server.origin}/mcp-npr"
    token = f"{GETGATHER_OAUTH_PROVIDER_NAME}_{app_key}_{user_id}"
    async with Client(url, auth=token) as client:
        result = await client.call_tool_mcp("npr_get_headlines", {})

    assert result.structuredContent is not None
    assert "headlines" in result.structuredContent
    assert len(result.structuredContent["headlines"]) > 0
