import asyncio

import pytest
from fastmcp import Client

from src.auth.auth import AuthUser
from src.auth.constants import GETGATHER_OAUTH_PROVIDER_NAME
from src.container.service import ContainerService
from src.settings import settings
from tests.conftest import ServerWithOrigin


@pytest.mark.asyncio
async def test_mcp_getgather_auth(server: ServerWithOrigin):
    user_id = "test_user_id"
    app_key, app_name = list(settings.GETGATHER_APPS.items())[0]
    url = f"{server.origin}/mcp-media"
    token = f"{GETGATHER_OAUTH_PROVIDER_NAME}_{app_key}_{user_id}"

    token = f"{GETGATHER_OAUTH_PROVIDER_NAME}_{app_key}_{user_id}"
    result = await _call_tool(url, "get_user_info", token)

    assert (
        result.structured_content
        == AuthUser(sub=user_id, auth_provider="getgather", app_name=app_name).dump()
    )


@pytest.mark.asyncio
async def test_mcp_github_auth(server: ServerWithOrigin):
    result = await _call_tool(
        f"{server.origin}/mcp-media", "get_user_info", settings.TEST_GITHUB_OAUTH_TOKEN
    )
    assert not result.is_error
    user = AuthUser.model_validate(result.structured_content)
    assert user.auth_provider == "github"


@pytest.mark.asyncio
async def test_npr(server: ServerWithOrigin):
    user_id = "test_user_id"
    app_key = list(settings.GETGATHER_APPS.keys())[0]
    result = await _call_tool(
        f"{server.origin}/mcp-npr",
        "npr_get_headlines",
        f"{GETGATHER_OAUTH_PROVIDER_NAME}_{app_key}_{user_id}",
    )

    assert result.structured_content is not None
    assert "headlines" in result.structured_content
    assert len(result.structured_content["headlines"]) > 0


@pytest.mark.asyncio
async def test_concurrent_requests(server: ServerWithOrigin):
    user_id = "test_user_id"
    app_key = list(settings.GETGATHER_APPS.keys())[0]
    calls = [
        _call_tool(
            f"{server.origin}/mcp-media",
            "get_user_info",
            f"{GETGATHER_OAUTH_PROVIDER_NAME}_{app_key}_{user_id}",
        )
        for _ in range(settings.MAX_NUM_RUNNING_CONTAINERS)
    ]
    await asyncio.gather(*calls)

    assigned_containers = await ContainerService.get_containers(
        partial_name=f"{user_id}.getgather", status="all"
    )
    assert len(assigned_containers) == 1

    unassigned_containers = await ContainerService.get_containers(
        partial_name="UNASSIGNED-", status="all"
    )
    assert len(unassigned_containers) == settings.MAX_NUM_RUNNING_CONTAINERS - 1


async def _call_tool(url: str, tool: str, token: str):
    async with Client(url, auth=token, timeout=60) as client:
        result = await client.call_tool(tool)
    return result
