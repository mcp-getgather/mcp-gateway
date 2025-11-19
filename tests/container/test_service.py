import asyncio
import platform
from typing import Any
from unittest.mock import patch

import pytest
from assertpy import assert_that

from src.auth.auth import AuthUser
from src.container.engine import engine_client
from src.container.manager import CONTAINER_NETWORK_NAME, Container
from src.container.service import ContainerIdentity, ContainerMetadata, ContainerService
from src.settings import settings


@pytest.mark.asyncio
async def test_create_new_container():
    container = await ContainerService.create_or_replace_container()

    await _assert_container_info(
        name=container.name,
        hostname=container.hostname,
        labels={
            "com.docker.compose.project": settings.CONTAINER_PROJECT_NAME,
            "com.docker.compose.service": "mcp-getgather",
        },
        state={
            "Status": "running",
            "Running": True,
        },
        env=[
            f"LOG_LEVEL={settings.LOG_LEVEL}",
            f"BROWSER_TIMEOUT={settings.BROWSER_TIMEOUT}",
            f"DEFAULT_PROXY_TYPE={settings.DEFAULT_PROXY_TYPE}",
            f"PROXIES_CONFIG={settings.PROXIES_CONFIG}",
            f"SENTRY_DSN={settings.CONTAINER_SENTRY_DSN}",
            f"DATA_DIR=/app/data",
            f"HOSTNAME={container.hostname}",
            "PORT=80",
        ],
        mount={
            "Type": "bind",
            "Source": str(container.mount_dir.resolve()),
            "Destination": "/app/data",
            "RW": True,
            "Propagation": "rprivate",
        },
    )
    await _assert_mount_dir(container.hostname)


@pytest.mark.asyncio
async def test_reload_unassigned_container():
    container = await ContainerService.create_or_replace_container()
    assert container is not None

    async with engine_client(network=CONTAINER_NETWORK_NAME, lock="write") as client:
        await client.delete_container(container.id)
    assert not await ContainerService.get_container(container.hostname)

    reloaded_container = await ContainerService.create_or_replace_container(
        mount_dir=container.mount_dir
    )

    _assert_same_container(container, reloaded_container)
    await _assert_mount_dir(reloaded_container.hostname)


@pytest.mark.asyncio
async def test_assign_container():
    container = await ContainerService.create_or_replace_container()
    user = AuthUser(sub="test_user", auth_provider="github")
    await _assign_container(user)

    container = await ContainerService.get_container(container.hostname)
    assert container is not None

    await _assert_container_info(
        name=container.name,
        hostname=container.hostname,
        user=user,
        mount={
            "Type": "bind",
            "Source": str(container.mount_dir.resolve()),
            "Destination": "/app/data",
            "RW": True,
            "Propagation": "rprivate",
        },
    )
    await _assert_mount_dir(container.hostname, user)


@pytest.mark.asyncio
async def test_reload_assigned_container():
    container = await ContainerService.create_or_replace_container()
    user = AuthUser(sub="test_user", auth_provider="github")
    await _assign_container(user)

    container = await ContainerService.get_container(container.hostname)
    assert container is not None

    async with engine_client(network=CONTAINER_NETWORK_NAME, lock="write") as client:
        await client.delete_container(container.id)
    assert not await ContainerService.get_container(container.hostname)

    reloaded_container = await ContainerService.create_or_replace_container(
        mount_dir=container.mount_dir
    )

    _assert_same_container(container, reloaded_container)
    await _assert_mount_dir(reloaded_container.hostname, user)


async def _assign_container(user: AuthUser) -> Container:
    # non-macOS systems need to wait for container to install iproute2 before assignment
    start_time_seconds = 5 if platform.system() != "Darwin" else 0
    await asyncio.sleep(start_time_seconds)

    with patch("src.container.service.CONTAINER_STARTUP_SECONDS", start_time_seconds):
        return await ContainerService.assign_container(user)


async def _assert_container_info(
    *,
    name: str,
    hostname: str,
    user: AuthUser | None = None,
    labels: dict[str, str] | None = None,
    state: dict[str, str | bool] | None = None,
    env: list[str] | None = None,
    mount: dict[str, str | bool] | None = None,
):
    container = await ContainerService.get_container(hostname)
    assert container is not None
    info = container.info

    assert container.info["Name"].lstrip("/") == name
    assert container.info["Config"]["Hostname"] == hostname

    assert container.name == ContainerIdentity(hostname=hostname, user=user).container_name

    if labels:
        assert info["Config"]["Labels"] == labels
    if state:
        assert_that(state).is_subset_of(info["State"])
    if env:
        assert_that(info["Config"]["Env"]).contains(*env)
    if mount:
        assert_that(mount).is_subset_of(info["Mounts"][0])

    network_name = f"{settings.CONTAINER_PROJECT_NAME}_internal-net"
    assert network_name in info["NetworkSettings"]["Networks"]


async def _assert_mount_dir(hostname: str, user: AuthUser | None = None) -> None:
    mount_dir = Container.mount_dir_for_hostname(hostname)
    assert mount_dir.exists()
    if user:
        metadata = await ContainerService.read_metadata(hostname)
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

        info["Config"].pop("Hostname", None)
        info["Config"].pop("CreateCommand", None)
        info["Config"]["Env"] = sorted(info["Config"]["Env"])

        for key in ["SandboxID", "SandboxKey"]:
            info["NetworkSettings"].pop(key, None)

        network_name = f"{settings.CONTAINER_PROJECT_NAME}_internal-net"
        for key in ["EndpointID", "MacAddress", "IPAddress"]:
            info["NetworkSettings"]["Networks"][network_name].pop(key, None)

        if "DNSNames" in info["NetworkSettings"]["Networks"][network_name]:
            info["NetworkSettings"]["Networks"][network_name]["DNSNames"].remove(container.id)
        if "Aliases" in info["NetworkSettings"]["Networks"][network_name]:
            if info["NetworkSettings"]["Networks"][network_name]["Aliases"]:
                info["NetworkSettings"]["Networks"][network_name]["Aliases"].remove(container.id)

        return info

    assert _pick_info(container_1) == _pick_info(container_2)
