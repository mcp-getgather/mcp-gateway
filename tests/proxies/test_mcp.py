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
    app_id = list(settings.GETGATHER_APP_IDS)[0]
    url = f"{settings.GATEWAY_ORIGIN}/mcp-media"
    headers = {"Authorization": f"Bearer {GETGATHER_OATUH_TOKEN_PREFIX}_{app_id}_{user_id}"}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get_user_info")

    assert result.structuredContent == AuthUser(
        sub=user_id, auth_provider="getgather", app_id=app_id
    ).model_dump(exclude_none=True)
