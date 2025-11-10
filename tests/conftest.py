import asyncio
import shutil
import subprocess
from typing import Literal

import aiorwlock
import pytest
import pytest_asyncio
from aiodocker import Docker
from aiodocker.networks import DockerNetwork

from src.container import engine
from src.container.engine import delete_container, get_container_engine_socket
from src.container.manager import CONTAINER_IMAGE_NAME, ContainerManager
from src.logs import logger
from src.settings import ENV_FILE, settings


@pytest_asyncio.fixture(autouse=True)
async def setup_container_engine_for_function():
    """Initialize and clean up a container engine environment for testing."""
    await _cleanup_container_engine(scope="function")

    yield

    await _cleanup_container_engine(scope="function")


@pytest_asyncio.fixture(autouse=True, scope="session")
async def setup_container_engine():
    """Initialize and clean up a docker environment for testing."""
    await _cleanup_container_engine(scope="session")
    await _init_container_engine()

    yield

    await _cleanup_container_engine(scope="session")


@pytest.fixture(autouse=True)
def reset_container_lock():
    """Reset the CONTAINER_LOCK for each test to avoid event loop binding issues."""
    engine.CONTAINER_LOCK = aiorwlock.RWLock()
    yield
    engine.CONTAINER_LOCK = aiorwlock.RWLock()


async def _run_cmd(cmd: str):
    process = await asyncio.create_subprocess_shell(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    output, error = await process.communicate()
    exit_status = process.returncode
    if exit_status != 0:
        raise RuntimeError(
            f"Command '{cmd}' failed with exit status: {exit_status}\n"
            f"Output: {output.decode('utf-8')}\n"
            f"Error: {error.decode('utf-8')}"
        )


async def _init_container_engine():
    """
    Initialize docker environment.
    Pull SERVER_IMAGE image and start services & networks in docker-compose.yml.
    """
    await ContainerManager.pull_container_image()

    cmd = f"DOCKER_HOST={get_container_engine_socket()} docker compose"
    if ENV_FILE:
        cmd += f" --env-file {ENV_FILE}"
    cmd += " up -d"
    await _run_cmd(cmd)


async def _cleanup_container_engine(scope: Literal["function", "session"]):
    """
    Cleanup docker environment.
    For function scope, only cleanup SERVER_IMAGE containers.
    For session scope, cleanup all CONTAINER_PROJECT_NAME containers, networks and images.
    """
    logger.info("Cleanup docker environment")
    docker = Docker(url=get_container_engine_socket())

    label_filters = [f"com.docker.compose.project={settings.CONTAINER_PROJECT_NAME}"]
    if scope == "function":
        label_filters.append(f"com.docker.compose.service=mcp-getgather")
    containers = await docker.containers.list(all=True, filters={"label": label_filters})  # type: ignore[reportUnknownMemberType]
    await asyncio.gather(*[delete_container(container) for container in containers])

    if scope == "session":
        network_data_list = await docker.networks.list(filters={"label": label_filters})
        for network_data in network_data_list:
            network = DockerNetwork(docker, network_data["Id"])
            await network.delete()

        try:
            await docker.images.delete(CONTAINER_IMAGE_NAME, force=True)
        except:
            pass  # ignore image not found error

    await docker.close()

    shutil.rmtree(settings.container_mount_parent_dir, ignore_errors=True)
