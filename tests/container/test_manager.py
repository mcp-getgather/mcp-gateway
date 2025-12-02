import asyncio
import platform
from collections.abc import AsyncGenerator
from typing import Callable, Literal, cast, overload
from unittest.mock import patch

import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from uvicorn import Server

from src.auth.auth import AuthUser
from src.auth.getgather_oauth_token import GETGATHER_OATUH_TOKEN_PREFIX
from src.container.container import Container
from src.container.engine import engine_client
from src.container.manager import (
    CallbackTTLCache,
    _cleanup_container,  # type: ignore[reportPrivateUsage]
    _get_total_num_containers,  # type: ignore[reportPrivateUsage]
)
from src.container.service import CONTAINER_LABELS, CONTAINER_NETWORK_NAME, UNASSIGNED_USER_ID
from src.settings import settings


# checkpoint/restore is only supported by podman
@pytest.mark.skipif(
    condition=(settings.CONTAINER_ENGINE != "podman" and platform.system() != "Darwin"),
    reason="Checkpoint/restore is only supported by podman on Linux",
)
@pytest.mark.asyncio
async def test_persistent_container_lifecycle(
    server_factory: Callable[[], AsyncGenerator[Server, None]],
):
    mock_pool = CallbackTTLCache[str, Container](
        maxsize=10,
        ttl=5,
        on_expire=_cleanup_container,
        on_pop=_cleanup_container,
    )

    with patch("src.container.manager._active_assigned_pool", mock_pool):
        # Step 1. start the server (done by the fixture)
        async for _ in server_factory():
            await _assert_container_pools(mock_pool)

            # Step 2. make a request from a github user
            user = await _make_mcp_request(settings.TEST_GITHUB_OAUTH_TOKEN)
            container_1 = await _assert_container_pools(
                mock_pool,
                user=user,
                assigned_container_status="active",
            )

            # Step 3. wait for the container to be checkpointed
            await asyncio.sleep(mock_pool.ttl * 3)
            container_2 = await _assert_container_pools(
                mock_pool,
                user=user,
                assigned_hostname=container_1.hostname,
                assigned_container_status="checkpointed",
            )

            # the container should be the same
            assert container_1.id == container_2.id
            assert container_1.name == container_2.name
            assert container_1.hostname == container_2.hostname

            # Step 4. make another request from the same user
            user = await _make_mcp_request(settings.TEST_GITHUB_OAUTH_TOKEN)
            container_3 = await _assert_container_pools(
                mock_pool,
                user=user,
                assigned_hostname=container_1.hostname,
                assigned_container_status="active",
            )

            # the container should be the same
            assert container_1.id == container_3.id
            assert container_1.name == container_3.name
            assert container_1.hostname == container_3.hostname


@pytest.mark.asyncio
async def test_one_time_container_lifecycle(
    server_factory: Callable[[], AsyncGenerator[Server, None]],
):
    mock_pool = CallbackTTLCache[str, Container](
        maxsize=10,
        ttl=5,
        on_expire=_cleanup_container,
        on_pop=_cleanup_container,
    )

    with patch("src.container.manager._active_assigned_pool", mock_pool):
        # Step 1. start the server (done by the fixture)
        async for _ in server_factory():
            await _assert_container_pools(mock_pool)

            # Step 2. make a request from a github user
            user_id = "test_user_id"
            app_key, app_name = list(settings.GETGATHER_APPS.items())[0]
            token = f"{GETGATHER_OATUH_TOKEN_PREFIX}_{app_key}_{user_id}"
            user = await _make_mcp_request(token)

            assert user.app_name == app_name
            container_1 = await _assert_container_pools(
                mock_pool, user=user, assigned_container_status="active"
            )

            # Step 3. wait for the container to be deleted
            await asyncio.sleep(mock_pool.ttl * 3)
            await _assert_container_pools(
                mock_pool,
                user=user,
                assigned_hostname=container_1.hostname,
                assigned_container_status="deleted",
            )

            # Step 4. make another request from the same user
            user = await _make_mcp_request(settings.TEST_GITHUB_OAUTH_TOKEN)
            container_2 = await _assert_container_pools(
                mock_pool, user=user, assigned_container_status="active"
            )

            # the container should be different
            assert container_1.id != container_2.id
            assert container_1.hostname != container_2.hostname


@overload
async def _assert_container_pools(
    pool: CallbackTTLCache[str, Container],
    *,
    user: AuthUser,
    assigned_hostname: str | None = None,
    assigned_container_status: Literal["active", "checkpointed", "deleted"] = "active",
) -> Container: ...


@overload
async def _assert_container_pools(
    pool: CallbackTTLCache[str, Container],
    *,
    user: None = None,
    assigned_hostname: None = None,
    assigned_container_status: Literal["active", "checkpointed", "deleted"] = "active",
) -> None: ...


async def _assert_container_pools(
    pool: CallbackTTLCache[str, Container],
    *,
    user: AuthUser | None = None,
    assigned_hostname: str | None = None,
    assigned_container_status: Literal["active", "checkpointed", "deleted"] = "active",
) -> Container | None:
    async with engine_client(network=CONTAINER_NETWORK_NAME) as client:
        containers = await client.list_containers(labels=CONTAINER_LABELS, status="all")

    unassigned_containers = [c for c in containers if c.name.startswith(UNASSIGNED_USER_ID)]
    assert len(unassigned_containers) == _get_total_num_containers() - (
        1 if user and assigned_container_status == "active" else 0
    )
    for container in unassigned_containers:
        assert container.status == "running"

    assigned_containers = [c for c in containers if not c.name.startswith(UNASSIGNED_USER_ID)]
    # Get the data of a CallbackTTLCache without triggering expiration or pop callbacks
    pool_data = cast(dict[str, Container], pool._Cache__data)  # type: ignore

    if not user:
        assert len(assigned_containers) == 0
        assert len(pool_data) == 0

        return None
    elif assigned_container_status == "deleted":
        assert len(assigned_containers) == 0
        assert len(pool_data) == 0

        assert user.auth_provider == "getgather"

        assert assigned_hostname is not None
        mount_dir = settings.container_mount_parent_dir / assigned_hostname
        assert not mount_dir.exists()
        cleanup_mount_dir = settings.cleanup_container_mount_parent_dir / assigned_hostname
        assert cleanup_mount_dir.exists()

        return None
    else:  # user is not None and expected_user_container_status is not "deleted"
        assert len(assigned_containers) == 1
        container = assigned_containers[0]
        if assigned_hostname:
            assert container.hostname == assigned_hostname

        if assigned_container_status == "active":
            assert len(pool_data) == 1

            assert container.status == "running"
            assert not container.checkpointed
            assert container.hostname in pool_data
        elif assigned_container_status == "checkpointed":
            assert len(pool_data) == 0

            assert user.auth_provider != "getgather"
            assert container.status == "exited"
            assert container.checkpointed

        return container


async def _make_mcp_request(auth_token: str):
    url = f"{settings.GATEWAY_ORIGIN}/mcp-media"
    headers = {"Authorization": f"Bearer {auth_token}"}

    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("get_user_info")

    return AuthUser.model_validate(result.structuredContent)
