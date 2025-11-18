import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from uvicorn import Server

from src.auth.auth import AuthUser
from src.auth.getgather_oauth_token import GETGATHER_OATUH_TOKEN_PREFIX
from src.settings import settings


@pytest.mark.asyncio
async def test_mcp_getgather_auth(server: Server):
    user_id = "test_user_id"
    app_key, app_name = list(settings.GETGATHER_APPS.items())[0]
    url = f"{settings.GATEWAY_ORIGIN}/mcp-media"
    headers = {"Authorization": f"Bearer {GETGATHER_OATUH_TOKEN_PREFIX}_{app_key}_{user_id}"}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get_user_info")

    assert result.structuredContent == AuthUser(
        sub=user_id, auth_provider="getgather", app_name=app_name
    ).model_dump(exclude_none=True)


@pytest.mark.asyncio
async def test_npr(server: Server):
    user_id = "test_user_id"
    app_key = list(settings.GETGATHER_APPS.keys())[0]
    url = f"{settings.GATEWAY_ORIGIN}/mcp-npr"
    headers = {"Authorization": f"Bearer {GETGATHER_OATUH_TOKEN_PREFIX}_{app_key}_{user_id}"}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("npr_get_headlines")

    assert result.structuredContent is not None
    assert "headlines" in result.structuredContent
    assert len(result.structuredContent["headlines"]) > 0
