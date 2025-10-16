import asyncio
import shutil
import subprocess
from typing import Literal

import aiorwlock
import pytest
import pytest_asyncio
from aiodocker import Docker
from aiodocker.networks import DockerNetwork

from src import server_manager
from src.logs import logger
from src.settings import ENV_FILE, settings


@pytest_asyncio.fixture(autouse=True)
async def setup_docker_for_function():
    """Initialize and clean up a docker environment for testing."""
    await _cleanup_docker(scope="function")

    yield

    await _cleanup_docker(scope="function")


@pytest_asyncio.fixture(autouse=True, scope="session")
async def setup_docker_for_session():
    """Initialize and clean up a docker environment for testing."""
    await _cleanup_docker(scope="session")
    await _init_docker()

    yield

    await _cleanup_docker(scope="session")


@pytest.fixture(autouse=True)
def reset_container_lock():
    """Reset the CONTAINER_LOCK for each test to avoid event loop binding issues."""
    server_manager.CONTAINER_LOCK = aiorwlock.RWLock()
    yield
    server_manager.CONTAINER_LOCK = aiorwlock.RWLock()


async def _run_cmd(cmd: str):
    process = await asyncio.create_subprocess_shell(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    await process.communicate()
    exit_status = process.returncode
    if exit_status != 0:
        raise RuntimeError(f"Command '{cmd}' failed with exit status: {exit_status}")


async def _init_docker():
    """
    Initialize docker environment.
    Pull SERVER_IMAGE image and start services & networks in docker-compose.yml.
    """
    docker = Docker()
    source_image = "ghcr.io/mcp-getgather/mcp-getgather:latest"
    await docker.images.pull(source_image)
    await docker.images.tag(source_image, repo=settings.SERVER_IMAGE)
    await docker.close()

    await _run_cmd(f"docker compose --profile default --env-file {ENV_FILE} up -d")


async def _cleanup_docker(scope: Literal["function", "session"]):
    """
    Cleanup docker environment.
    For function scope, only cleanup SERVER_IMAGE containers.
    For session scope, cleanup all DOCKER_PROJECT_NAME containers, networks and images.
    """
    logger.info("Cleanup docker environment")
    docker = Docker()

    label_filter = [f"com.docker.compose.project={settings.DOCKER_PROJECT_NAME}"]
    container_filters = {"label": label_filter}
    if scope == "function":
        container_filters["ancestor"] = [settings.SERVER_IMAGE]
    containers = await docker.containers.list(all=True, filters=container_filters)  # type: ignore[reportUnknownMemberType]
    for container in containers:
        await container.delete(force=True)  # type: ignore[reportUnknownMemberType]

    if scope == "session":
        network_data_list = await docker.networks.list(filters={"label": label_filter})
        for network_data in network_data_list:
            network = DockerNetwork(docker, network_data["Id"])
            await network.delete()

        try:
            await docker.images.delete(settings.SERVER_IMAGE, force=True)
        except:
            pass  # ignore image not found error

    await docker.close()

    shutil.rmtree(settings.server_mount_parent_dir, ignore_errors=True)
