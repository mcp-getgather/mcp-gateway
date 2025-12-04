import asyncio
import shutil
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Callable, Literal

import aiorwlock
import httpx
import pytest
import pytest_asyncio
from loguru import logger
from uvicorn import Server

from src.container import engine
from src.container.container import Container
from src.container.engine import (
    ContainerEngineClient,
    engine_client,
    get_container_engine_socket,
    run_cli,
)
from src.container.manager import ContainerManager
from src.container.service import (
    CONTAINER_IMAGE_NAME,
    CONTAINER_NETWORK_NAME,
    CONTAINER_STARTUP_SECONDS,
    ContainerService,
)
from src.main import create_server
from src.settings import ENV_FILE, settings


@pytest_asyncio.fixture(scope="function")
async def server(server_factory: Callable[[], AsyncGenerator[Server, None]]):
    async for server in server_factory():
        yield server
        break


@pytest.fixture(scope="function")
def server_factory() -> Callable[[], AsyncGenerator[Server, None]]:
    async def _create_server() -> AsyncGenerator[Server, None]:
        server = await create_server()
        server_task = asyncio.create_task(server.serve())

        # wait for server to start
        url = f"{settings.GATEWAY_ORIGIN}/health"
        end_time = time.time() + CONTAINER_STARTUP_SECONDS + 5

        try:
            while time.time() < end_time:
                try:
                    async with httpx.AsyncClient() as client:
                        response = await client.get(url, timeout=1.0)
                        if response.is_success:
                            yield server
                            break
                except httpx.RequestError:
                    pass
                time.sleep(1)
            else:
                raise RuntimeError("Server failed to start")
        except Exception as e:
            raise e
        finally:
            server.should_exit = True
            await server_task

    return _create_server


@pytest.fixture(scope="function")
def created_container_hostnames() -> set[str]:
    """Track the hostnames of containers created in this test function."""
    return set()


@pytest_asyncio.fixture(autouse=True)
async def setup_container_engine_for_function(created_container_hostnames: set[str]):
    await _cleanup_container_engine(
        scope="function", created_container_hostnames=created_container_hostnames
    )
    yield
    await _cleanup_container_engine(
        scope="function", created_container_hostnames=created_container_hostnames
    )


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
    engine.CONTAINER_ENGINE_LOCK = aiorwlock.RWLock()
    yield
    engine.CONTAINER_ENGINE_LOCK = aiorwlock.RWLock()


@pytest.fixture(autouse=True)
def track_created_containers(
    monkeypatch: pytest.MonkeyPatch, created_container_hostnames: set[str]
):
    original_create_or_replace = ContainerService._create_or_replace_container_impl  # type: ignore[reportPrivateUsage]

    async def tracked_create_or_replace(
        cls: type[ContainerManager], client: ContainerEngineClient, *, mount_dir: Path | None = None
    ) -> Container:
        """Wrapper that tracks created containers."""
        container = await original_create_or_replace(client, mount_dir=mount_dir)

        # Track the created container hostname
        created_container_hostnames.add(container.hostname)

        return container

    monkeypatch.setattr(
        ContainerService,
        "_create_or_replace_container_impl",
        classmethod(tracked_create_or_replace),
    )


async def _init_container_engine():
    """
    Initialize docker environment.
    Pull SERVER_IMAGE image and start services & networks in docker-compose.yml.
    """
    await ContainerService.pull_container_image()

    cmd = "docker compose"
    if ENV_FILE:
        cmd += f" --env-file {ENV_FILE}"
    cmd += " up -d"
    await run_cli("sh", "-c", cmd, env=_get_compose_env(), timeout=10)


async def _cleanup_container_engine(
    scope: Literal["function", "session"], created_container_hostnames: set[str] | None = None
):
    """
    Cleanup docker environment.
    For function scope, only cleanup containers created in the current test function.
    For session scope, cleanup all CONTAINER_PROJECT_NAME containers, networks and images.
    """
    logger.info("Cleanup docker environment")
    async with engine_client(network=CONTAINER_NETWORK_NAME, lock="write") as client:
        labels = {"com.docker.compose.project": settings.CONTAINER_PROJECT_NAME}
        if scope == "function":
            labels["com.docker.compose.service"] = "mcp-getgather"
            for hostname in created_container_hostnames or {}:
                container = await ContainerService.get_container(hostname, client=client)
                if container:
                    await ContainerService.purge_container(container, client=client)
        else:
            containers = await client.list_containers(labels=labels)
            await client.delete_containers(*[container.id for container in containers])
            await asyncio.to_thread(
                shutil.rmtree, settings.container_mount_parent_dir, ignore_errors=True
            )

    if scope == "session":
        cmd = "docker compose"
        if ENV_FILE:
            cmd += f" --env-file {ENV_FILE}"
        cmd += " down"
        await run_cli("sh", "-c", cmd, env=_get_compose_env(), timeout=20)

        try:
            await client.delete_image(CONTAINER_IMAGE_NAME)
        except:
            pass  # ignore image not found error


def _get_compose_env():
    return {
        "CONTAINER_PROJECT_NAME": settings.CONTAINER_PROJECT_NAME,
        "TS_AUTHKEY": "",
        "CONTAINER_SUBNET_PREFIX": settings.CONTAINER_SUBNET_PREFIX,
        "DOCKER_HOST": get_container_engine_socket(settings.CONTAINER_ENGINE),
    }
