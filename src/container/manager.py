import asyncio
import platform
import random
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import aiofiles
from nanoid import generate
from pydantic import BaseModel

from src.auth.auth import AuthUser
from src.container.container import Container
from src.container.engine import ContainerEngineClient, engine_client
from src.logs import logger
from src.settings import settings

UNASSIGNED_USER_ID = "UNASSIGNED"
FRIENDLY_CHARS: str = "23456789abcdefghijkmnpqrstuvwxyz"

# "internal-net" is the network name used in docker-compose.yml
# the full network name is prefixed by settings.CONTAINER_PROJECT_NAME
CONTAINER_NETWORK_NAME = f"{settings.CONTAINER_PROJECT_NAME}_internal-net"

CONTAINER_IMAGE_NAME = f"{settings.CONTAINER_PROJECT_NAME}_mcp-getgather"
CONTAINER_STARTUP_SECONDS = 20


class ContainerMetadata(BaseModel):
    user: AuthUser


class ContainerManager:
    """
    Manages the lifecycle and routing of containers.

    === Containers ===
    - Containers run the CONTAINER_IMAGE_NAME service in the same network as the gateway.
    - Container identifiers:
      - CONTAINER_ID: the id of the container, auto created by Docker. It changes after reload / restart.
      - HOSTNAME: the unique identifier of the container through the whole lifecycle.
        It is a random nanoid string generated at container creation.
        It's also the host mount directory name for /app/data.
      - CONTAINER_NAME: the name of the container, UNASSIGNED-[HOSTNAME] for unassigned containers
        and [USER_ID]-[HOSTNAME] for assigned containers, where assigned USER_ID is AuthUser.user_id.
    - Containers can be searched by USER_ID or HOSTNAME since they are unique and substrings of CONTAINER_NAME.
    - Container IP address is used to route requests to the container.

    === Lifecyle and Routing ===
    - The pool maintains a list of settings.MIN_CONTAINER_POOL_SIZE unassigned containers.
    - When a new user connects, the manager will assign a container from the pool to the user, and backfill the pool.
      Assignment updates the container name from UNASSIGNED-[HOSTNAME] to [USER_ID]-[HOSTNAME].
    - Containers can also be reloaded on demand, in order to update the container image.
    """

    @classmethod
    async def get_user_container(cls, user: AuthUser) -> Container:
        """Get the container of the user. Assign one if not exists."""
        container = await cls._get_container(user.user_id)
        if not container:
            async with engine_client(network=CONTAINER_NETWORK_NAME, lock="write") as client:
                container = await cls._assign_container(user, client=client)
                await cls.backfill_container_pool(client=client)
        return container

    @classmethod
    async def get_container_by_hostname(cls, hostname: str) -> Container:
        container = await cls._get_container(hostname)
        if not container:
            raise ValueError(f"Container {hostname} not found")
        return container

    @classmethod
    async def get_unassigned_container(cls) -> Container:
        return await cls._get_random_unassigned_container()

    @classmethod
    async def reload_containers(cls, *, state: Literal["all", "stopped"] = "stopped"):
        mount_dirs = cls._get_mount_dirs()
        async with engine_client(network=CONTAINER_NETWORK_NAME, lock="write") as client:
            if state == "stopped":
                running_containers = await cls._get_containers(client=client)
                running_dirs = set(container.mount_dir for container in running_containers)
                mount_dirs = [item for item in mount_dirs if item not in running_dirs]

            for item in mount_dirs:
                await cls._create_or_replace_container(mount_dir=item, client=client)
            logger.info(f"Reloaded {len(mount_dirs)} containers")

            await cls.backfill_container_pool(client=client)

    @classmethod
    async def backfill_container_pool(cls, *, client: ContainerEngineClient | None = None):
        async with engine_client(
            client=client, network=CONTAINER_NETWORK_NAME, lock="write"
        ) as _client:
            containers = await cls._get_containers(
                partial_name=UNASSIGNED_USER_ID, client=_client, only_ready=False
            )
            num = settings.MIN_CONTAINER_POOL_SIZE - len(containers)
            if num <= 0:
                return
            logger.info(f"Backfill container pool with {num} containers")

            # run sequentially to avoid overwhelming the container engine
            for _ in range(num):
                await cls._create_or_replace_container(client=_client)

    @classmethod
    async def pull_container_image(cls):
        source_image = "ghcr.io/mcp-getgather/mcp-getgather:latest"
        logger.info(f"Pulling container image from {source_image}")
        async with engine_client(network=CONTAINER_NETWORK_NAME) as client:
            await client.pull_image(source_image, tag=CONTAINER_IMAGE_NAME)

    @classmethod
    async def _get_containers(
        cls,
        *,
        partial_name: str | None = None,
        client: ContainerEngineClient | None = None,
        only_ready: bool = True,
    ) -> list[Container]:
        async with engine_client(
            client=client, network=CONTAINER_NETWORK_NAME, lock="read"
        ) as _client:
            containers = await _client.list_containers(
                partial_name=partial_name,
                labels={
                    "com.docker.compose.project": settings.CONTAINER_PROJECT_NAME,
                    "com.docker.compose.service": "mcp-getgather",
                },
            )
        if only_ready:
            containers = [c for c in containers if await cls._is_container_ready(c)]
        return containers

    @classmethod
    async def _get_container(
        cls, partial_name: str, *, client: ContainerEngineClient | None = None
    ) -> Container | None:
        containers = await cls._get_containers(
            partial_name=partial_name, client=client, only_ready=False
        )
        if len(containers) > 1:
            raise ValueError(
                f"Multiple containers found for {partial_name}, use _get_containers instead"
            )
        return containers[0] if containers else None

    @classmethod
    async def _get_random_unassigned_container(cls, client: ContainerEngineClient | None = None):
        containers = await cls._get_containers(partial_name=UNASSIGNED_USER_ID, client=client)
        if not containers:
            raise RuntimeError("No unassigned containers found")
        container = random.choice(containers)
        logger.info(f"Randomly selected unassigned container {container.id}")
        return container

    @classmethod
    def _get_mount_dirs(cls):
        return [item for item in settings.container_mount_parent_dir.iterdir() if item.is_dir()]

    @classmethod
    def _generate_container_name(cls) -> str:
        """Generate a random name until it is not in the existing names."""
        mount_dirs = cls._get_mount_dirs()
        existing_names = set(item.stem for item in mount_dirs)
        while (name := generate(FRIENDLY_CHARS, 6)) in existing_names:
            continue
        return name

    @classmethod
    async def _create_or_replace_container(
        cls, *, mount_dir: Path | None = None, client: ContainerEngineClient | None = None
    ):
        # catch all exceptions in _create_or_replace_container_impl,
        # docker_client handles raise based on whether it's nested or not
        async with engine_client(
            client=client, network=CONTAINER_NETWORK_NAME, lock="write"
        ) as _client:
            try:
                return await cls._create_or_replace_container_impl(_client, mount_dir=mount_dir)
            except Exception as e:
                logger.error(
                    f"Failed to create or reload container for mount_dir: {mount_dir}: {e}"
                )
                raise e

    @classmethod
    async def _create_or_replace_container_impl(
        cls, client: ContainerEngineClient, *, mount_dir: Path | None = None
    ):
        """Create a fresh container for UNASSIGNED user or load from a mount_dir for an existing user."""
        if mount_dir is None:
            hostname = cls._generate_container_name()
            user = None
        else:
            hostname = mount_dir.stem
            metadata = await cls._read_metadata(hostname)
            user = metadata.user if metadata else None

        container_name = cls._container_name_for_user(hostname, user=user)

        src_data_dir = str(Container.mount_dir_for_hostname(hostname).resolve())
        dst_data_dir = "/app/data"

        env = {
            "ENVIRONMENT": settings.GATEWAY_ORIGIN,
            "LOGFIRE_TOKEN": settings.LOGFIRE_TOKEN,
            "LOG_LEVEL": settings.LOG_LEVEL,
            "HOSTNAME": hostname,
            "BROWSER_TIMEOUT": settings.BROWSER_TIMEOUT,
            "DEFAULT_PROXY_TYPE": settings.DEFAULT_PROXY_TYPE,
            "PROXIES_CONFIG": settings.PROXIES_CONFIG,
            "SENTRY_DSN": settings.CONTAINER_SENTRY_DSN,
            "DATA_DIR": dst_data_dir,
            "PORT": "80",
        }
        labels = {
            "com.docker.compose.project": settings.CONTAINER_PROJECT_NAME,
            "com.docker.compose.service": "mcp-getgather",
        }
        cap_add = ["NET_BIND_SERVICE"]
        cmd = None

        # If host is not macOS, container needs the tailscale router to access proxy service,
        # so we need to override the entrypoint to install iproute2 and add tailscale routing.
        # The container also needs NET_ADMIN capabilities
        if platform.system() != "Darwin":
            cmd = [
                "/bin/sh",
                "-c",
                f"ip route add 100.64.0.0/10 via {cls._tailscale_router_ip()} && exec /app/entrypoint.sh",
            ]
            cap_add.append("NET_ADMIN")

        container = await client.create_or_replace_container(
            name=container_name,
            hostname=hostname,
            user="root",
            image=CONTAINER_IMAGE_NAME,
            envs=env,
            volumes=[f"{src_data_dir}:{dst_data_dir}:rw"],
            labels=labels,
            cap_adds=cap_add,
            cmd=cmd,
        )
        logger.info(f"Created or reloaded container hostname: {hostname}, id: {container.id}")
        return hostname

    @classmethod
    async def _assign_container(
        cls, user: AuthUser, *, client: ContainerEngineClient | None = None
    ):
        async with engine_client(
            client=client, network=CONTAINER_NETWORK_NAME, lock="write"
        ) as _client:
            # rename the container to [AuthUser.user_id]-[HOSTNAME]
            container = await cls._get_random_unassigned_container(client)
            assigned_container_name = cls._container_name_for_user(container.hostname, user=user)
            await _client.rename_container(container.id, assigned_container_name)

            await cls._write_metadata(container, user)

            # if platform.system() != "Darwin":
            #     exec = await _client.exec([
            #         "ip",
            #         "route",
            #         "add",
            #         "100.64.0.0/10",
            #         "via",
            #         cls._tailscale_router_ip(),
            #     ])
            #     await exec.start(detach=True)

            logger.info(f"Assigned container {container.id} to {user.user_id}")

        return container

    @classmethod
    async def _purge_container(cls, hostname: str, *, client: ContainerEngineClient | None = None):
        async with engine_client(
            client=client, network=CONTAINER_NETWORK_NAME, lock="write"
        ) as _client:
            container = await _client.get_container(name=hostname)
            await _client.delete_container(container.id)
            await asyncio.to_thread(shutil.rmtree, container.mount_dir)

    @classmethod
    async def _is_container_ready(cls, container: Container) -> bool:
        if container.status != "running":
            return False

        return datetime.now(timezone.utc) > container.started_at + timedelta(
            seconds=CONTAINER_STARTUP_SECONDS
        )

    @classmethod
    def _container_name_for_user(cls, hostname: str, *, user: AuthUser | None = None) -> str:
        return f"{user.user_id if user else UNASSIGNED_USER_ID}-{hostname}"

    @classmethod
    async def _write_metadata(cls, container: Container, user: AuthUser):
        metadata = ContainerMetadata(user=user)
        async with aiofiles.open(container.metadata_file, "w") as f:
            await f.write(metadata.model_dump_json())

    @classmethod
    async def _read_metadata(cls, hostname: str) -> ContainerMetadata | None:
        path = Container.metadata_file_for_hostname(hostname)
        if not path.exists():
            return None  # ignore unassigned container

        async with aiofiles.open(path, "r") as f:
            metadata = ContainerMetadata.model_validate_json(await f.read())
        return metadata

    @classmethod
    def _tailscale_router_ip(cls):
        """IP address of the tailscale router for accessing proxy service."""
        return f"{settings.CONTAINER_SUBNET_PREFIX}.2"
