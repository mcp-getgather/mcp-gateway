import asyncio
import platform
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from assertpy import assert_that

from src.auth import AuthUser
from src.server_manager import Container, ContainerMetadata, ServerManager, docker_client
from src.settings import settings


@pytest.mark.asyncio
async def test_create_new_container():
    hostname = await ServerManager._create_or_replace_container()  # type: ignore[reportPrivateUsage]

    await _assert_container_info(
        hostname=hostname,
        config={
            "Image": settings.SERVER_IMAGE,
            "Labels": {
                "com.docker.compose.project": settings.DOCKER_PROJECT_NAME,
                "com.docker.compose.service": "mcp-getgather",
            },
        },
        state={
            "Status": "running",
            "Running": True,
        },
        env=[
            f"LOG_LEVEL={settings.LOG_LEVEL}",
            "BROWSER_TIMEOUT=300000",
            f"BROWSER_HTTP_PROXY={settings.BROWSER_HTTP_PROXY}",
            f"BROWSER_HTTP_PROXY_PASSWORD={settings.BROWSER_HTTP_PROXY_PASSWORD}",
            f"OPENAI_API_KEY={settings.OPENAI_API_KEY}",
            f"SENTRY_DSN={settings.SERVER_SENTRY_DSN}",
            f"DATA_DIR=/app/data",
            f"HOSTNAME={hostname}",
            "PORT=80",
        ],
        mount={
            "Type": "bind",
            "Source": str(Container.mount_dir_for_hostname(hostname).resolve()),
            "Destination": "/app/data",
            "Mode": "rw",
            "RW": True,
            "Propagation": "rprivate",
        },
        network_aliases=[f"{hostname}"],
    )
    await _assert_mount_dir(hostname)


@pytest.mark.asyncio
async def test_reload_unassigned_container():
    hostname = await ServerManager._create_or_replace_container()  # type: ignore[reportPrivateUsage]
    mount_dir = Container.mount_dir_for_hostname(hostname)
    container = await ServerManager._get_container(hostname)  # type: ignore[reportPrivateUsage]

    async with docker_client() as docker:
        _container = await docker.containers.get(container.id)  # type: ignore[reportUnknownMemberType]
        await _container.delete(force=True)  # type: ignore[reportUnknownMemberType]
    assert not await ServerManager._get_container(hostname)  # type: ignore[reportPrivateUsage]

    reloaded_hostname = await ServerManager._create_or_replace_container(mount_dir=mount_dir)  # type: ignore[reportPrivateUsage]
    reloaded_container = await ServerManager._get_container(hostname)  # type: ignore[reportPrivateUsage]

    assert reloaded_hostname == hostname

    assert container is not None
    assert reloaded_container is not None

    _assert_same_container(container, reloaded_container)
    await _assert_mount_dir(hostname)


@pytest.mark.asyncio
async def test_assign_container():
    hostname = await ServerManager._create_or_replace_container()  # type: ignore[reportPrivateUsage]
    user = AuthUser(sub="test_user", auth_provider="github")
    await _assign_container(user)

    await _assert_container_info(
        hostname=hostname,
        user=user,
        mount={
            "Type": "bind",
            "Source": str(Container.mount_dir_for_hostname(hostname).resolve()),
            "Destination": "/app/data",
            "Mode": "rw",
            "RW": True,
            "Propagation": "rprivate",
        },
        network_aliases=[f"{hostname}"],
    )
    await _assert_mount_dir(hostname, user)


@pytest.mark.asyncio
async def test_reload_assigned_container():
    hostname = await ServerManager._create_or_replace_container()  # type: ignore[reportPrivateUsage]
    user = AuthUser(sub="test_user", auth_provider="github")
    await _assign_container(user)

    mount_dir = Container.mount_dir_for_hostname(hostname)
    container = await ServerManager._get_container(hostname)  # type: ignore[reportPrivateUsage]

    async with docker_client() as docker:
        _container = await docker.containers.get(container.id)  # type: ignore[reportUnknownMemberType]
        await _container.delete(force=True)  # type: ignore[reportUnknownMemberType]
    assert not await ServerManager._get_container(hostname)  # type: ignore[reportPrivateUsage]

    reloaded_hostname = await ServerManager._create_or_replace_container(mount_dir=mount_dir)  # type: ignore[reportPrivateUsage]
    reloaded_container = await ServerManager._get_container(hostname)  # type: ignore[reportPrivateUsage]

    assert reloaded_hostname == hostname

    assert container is not None
    assert reloaded_container is not None

    _assert_same_container(container, reloaded_container)
    await _assert_mount_dir(hostname, user)


async def _assign_container(user: AuthUser) -> None:
    # non-macOS systems need to wait for container to install iproute2 before assignment
    start_time_seconds = 5 if platform.system() != "Darwin" else 0
    await asyncio.sleep(start_time_seconds)

    with patch("src.server_manager.CONTAINER_STARTUP_TIME", timedelta(seconds=start_time_seconds)):
        await ServerManager._assign_container(user)  # type: ignore[reportPrivateUsage]


async def _assert_container_info(
    *,
    hostname: str,
    user: AuthUser | None = None,
    config: dict[str, Any] | None = None,
    state: dict[str, str | bool] | None = None,
    env: list[str] | None = None,
    mount: dict[str, str | bool] | None = None,
    network_aliases: list[str] | None = None,
):
    async with docker_client() as docker:
        container_name = Container.name_for_user(user, hostname)
        container = await docker.containers.get(container_name)  # type: ignore[reportUnknownMemberType]

        info = container._container  # type: ignore[reportPrivateUsage]

    assert info["Name"] == f"/{container_name}"

    if config:
        assert_that(config).is_subset_of(info["Config"])
    if state:
        assert_that(state).is_subset_of(info["State"])
    if env:
        assert_that(info["Config"]["Env"]).contains(*env)
    if mount:
        assert_that(info["Mounts"]).contains(mount)
    if network_aliases:
        network_name = f"{settings.DOCKER_PROJECT_NAME}_internal-net"
        assert_that(info["NetworkSettings"]["Networks"][network_name]["Aliases"]).contains(
            *network_aliases
        )


async def _assert_mount_dir(hostname: str, user: AuthUser | None = None) -> None:
    mount_dir = Container.mount_dir_for_hostname(hostname)
    assert mount_dir.exists()
    if user:
        metadata = await ServerManager._read_metadata(hostname)  # type: ignore[reportPrivateUsage]
        assert metadata == ContainerMetadata(user=user)
    else:
        assert not Container.metadata_file_for_hostname(hostname).exists()


def _assert_same_container(container_1: Container, container_2: Container) -> None:
    def _pick_info(container: Container) -> dict[str, Any]:
        # remove info that could change between reloads
        info = {
            k: v
            for k, v in container.info.items()
            if k in ["Name", "Image", "Config", "Mounts", "NetworkSettings"]
        }

        info["Config"].pop("Hostname")
        for key in ["SandboxID", "SandboxKey"]:
            info["NetworkSettings"].pop(key)

        network_name = f"{settings.DOCKER_PROJECT_NAME}_internal-net"
        for key in ["EndpointID", "MacAddress"]:
            info["NetworkSettings"]["Networks"][network_name].pop(key)
        info["NetworkSettings"]["Networks"][network_name]["DNSNames"].remove(container.id)

        return info

    assert _pick_info(container_1) == _pick_info(container_2)
