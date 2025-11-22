import asyncio
import platform
import random
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import aiofiles
from loguru import logger
from nanoid import generate
from pydantic import BaseModel, model_validator

from src.auth.auth import AuthUser
from src.container.container import Container
from src.container.engine import ContainerEngineClient, engine_client
from src.settings import settings

logger = logger.bind(topic="service")

UNASSIGNED_USER_ID = "UNASSIGNED"
FRIENDLY_CHARS: str = "23456789abcdefghijkmnpqrstuvwxyz"

# "internal-net" is the network name used in docker-compose.yml
# the full network name is prefixed by settings.CONTAINER_PROJECT_NAME
CONTAINER_NETWORK_NAME = f"{settings.CONTAINER_PROJECT_NAME}_internal-net"

CONTAINER_IMAGE_NAME = f"{settings.CONTAINER_PROJECT_NAME}_mcp-getgather"
CONTAINER_STARTUP_SECONDS = 20

CONTAINER_LABELS = {
    "com.docker.compose.project": settings.CONTAINER_PROJECT_NAME,
    "com.docker.compose.service": "mcp-getgather",
}


class ContainerIdentity(BaseModel):
    """Utility to convert between various container name, hostname, assigned user, etc."""

    hostname: str
    user_id: str | Literal["UNASSIGNED"] = UNASSIGNED_USER_ID
    user: AuthUser | None = None

    @model_validator(mode="before")
    @classmethod
    def validate_user(cls, data: dict[str, Any]) -> dict[str, Any]:
        user_id = data.get("user_id", None)
        user = data.get("user", None)
        if user:
            if user_id:
                assert user.user_id == user_id
            else:
                data["user_id"] = user.user_id
        return data

    @property
    def container_name(self) -> str:
        return f"{self.user_id}-{self.hostname}"

    @property
    def is_assigned_to_authenticated_user(self) -> bool:
        """Return True if the container is assigned to a non-getgather user."""
        return bool(self.user and self.user.auth_provider != "getgather")

    @property
    def is_assigned_to_getgather_app(self) -> bool:
        """Return True if the container is assigned to the one of the getgather apps."""
        return bool(self.user and self.user.auth_provider == "getgather")

    @classmethod
    async def from_hostname(cls, hostname: str) -> "ContainerIdentity":
        metadata = await ContainerService.read_metadata(hostname)
        if not metadata:
            return cls(hostname=hostname)
        return cls(hostname=hostname, user_id=metadata.user.user_id, user=metadata.user)

    @classmethod
    async def from_user(cls, user: AuthUser) -> "ContainerIdentity | None":
        name = await ContainerService.get_container_name(user.user_id)
        if not name:
            raise RuntimeError(f"Container for user {user} not found")
        return cls(hostname=name.split("-")[-1], user_id=user.user_id, user=user)

    @classmethod
    async def from_container_name(cls, name: str) -> "ContainerIdentity":
        parts = name.split("-")
        hostname = parts[-1]
        return await cls.from_hostname(hostname)


class ContainerMetadata(BaseModel):
    user: AuthUser


class ContainerService:
    """
    Tools to manage the lifecycle and routing of containers.

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
    async def pull_container_image(cls):
        source_image = "ghcr.io/mcp-getgather/mcp-getgather:latest"
        logger.info(f"Pulling container image", source=source_image)
        async with engine_client(network=CONTAINER_NETWORK_NAME) as client:
            await client.pull_image(source_image, tag=CONTAINER_IMAGE_NAME)

    @classmethod
    async def get_container_name(
        cls, partial_name: str, *, client: ContainerEngineClient | None = None
    ) -> str | None:
        async with engine_client(
            client=client, network=CONTAINER_NETWORK_NAME, lock="read"
        ) as _client:
            containers = await _client.list_containers_basic(
                partial_name=partial_name, labels=CONTAINER_LABELS
            )
            if len(containers) > 1:
                raise RuntimeError(f"Found multiple containers found for {partial_name}")
            return containers[0].name if containers else None

    @classmethod
    async def get_containers(
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
                partial_name=partial_name, labels=CONTAINER_LABELS
            )
        if only_ready:
            containers = [c for c in containers if await cls._is_container_ready(c)]
        return containers

    @classmethod
    async def get_container(
        cls, partial_name: str, *, client: ContainerEngineClient | None = None
    ) -> Container | None:
        containers = await cls.get_containers(
            partial_name=partial_name, client=client, only_ready=False
        )
        if len(containers) > 1:
            raise ValueError(
                f"Multiple containers found for {partial_name}, use _get_containers instead"
            )
        return containers[0] if containers else None

    @classmethod
    async def get_random_unassigned_container(cls, client: ContainerEngineClient | None = None):
        containers = await cls.get_containers(partial_name=UNASSIGNED_USER_ID, client=client)
        if not containers:
            raise RuntimeError("No unassigned containers found")
        container = random.choice(containers)
        logger.info(f"Randomly selected unassigned container", container=container.dump())
        return container

    @classmethod
    async def assign_container(cls, user: AuthUser, *, client: ContainerEngineClient | None = None):
        async with engine_client(
            client=client, network=CONTAINER_NETWORK_NAME, lock="write"
        ) as _client:
            # rename the container to [AuthUser.user_id]-[HOSTNAME]
            container = await cls.get_random_unassigned_container(client)
            assigned_container_name = ContainerIdentity(
                hostname=container.hostname, user=user
            ).container_name
            await _client.rename_container(container.id, assigned_container_name)

            # refresh the container object
            container = await _client.get_container(id=container.id)

            await cls._write_metadata(container, user)

            logger.info("Container assigned to user", container=container.dump(), user=user.dump())

            return container

    @classmethod
    async def purge_container(
        cls, container: Container, *, client: ContainerEngineClient | None = None
    ):
        """Delete a container and move its mount directory to the __cleanup directory."""
        async with engine_client(
            client=client, network=CONTAINER_NETWORK_NAME, lock="write"
        ) as _client:
            await _client.delete_container(container.id)
            dst_dir = settings.cleanup_container_mount_parent_dir / container.mount_dir.name
            await asyncio.to_thread(shutil.move, container.mount_dir, dst_dir)
            logger.info(
                f"Purged container removed its mount dir",
                container=container.dump(),
                cleanup_dir=dst_dir.as_posix(),
            )

    @classmethod
    async def checkpoint_container(
        cls, container: Container, *, client: ContainerEngineClient | None = None
    ):
        async with engine_client(
            client=client, network=CONTAINER_NETWORK_NAME, lock="write"
        ) as _client:
            await _client.disconnect_network(CONTAINER_NETWORK_NAME, container.id)
            await _client.checkpoint_container(container.id)

            # refresh the container object
            container = await _client.get_container(id=container.id)
            logger.info(f"Checkpointed container", container=container.dump())

            return container

    @classmethod
    async def restore_container(
        cls, container: Container, *, client: ContainerEngineClient | None = None
    ):
        async with engine_client(
            client=client, network=CONTAINER_NETWORK_NAME, lock="write"
        ) as _client:
            await _client.restore_container(container.id)
            await _client.connect_network(CONTAINER_NETWORK_NAME, container.id)

            # refresh the container object
            container = await _client.get_container(id=container.id)
            logger.info(f"Restored container", container=container.dump())

            return container

    @classmethod
    async def create_or_replace_container(
        cls, *, mount_dir: Path | None = None, client: ContainerEngineClient | None = None
    ) -> Container:
        # catch all exceptions in _create_or_replace_container_impl,
        # docker_client handles raise based on whether it's nested or not
        async with engine_client(
            client=client, network=CONTAINER_NETWORK_NAME, lock="write"
        ) as _client:
            try:
                return await cls._create_or_replace_container_impl(_client, mount_dir=mount_dir)
            except Exception as e:
                logger.exception(
                    f"Failed to create or reload container", error=e, mount_dir=mount_dir
                )
                raise e

    @classmethod
    async def _create_or_replace_container_impl(
        cls, client: ContainerEngineClient, *, mount_dir: Path | None = None
    ) -> Container:
        """Create a fresh container for UNASSIGNED user or load from a mount_dir for an existing user."""
        if mount_dir is None:
            hostname = cls._generate_container_hostname()
            user = None
        else:
            hostname = mount_dir.stem
            metadata = await cls.read_metadata(hostname)
            user = metadata.user if metadata else None

        container_name = ContainerIdentity(hostname=hostname, user=user).container_name

        src_data_dir = str(Container.mount_dir_for_hostname(hostname).resolve())
        dst_data_dir = "/app/data"

        # Mount proxies.yaml from host to container
        from src.settings import PROJECT_DIR
        src_proxies_file = str((PROJECT_DIR / "proxies.yaml").resolve())
        dst_proxies_file = "/app/proxies.yaml"

        env = {
            "ENVIRONMENT": settings.GATEWAY_ORIGIN,
            "LOGFIRE_TOKEN": settings.LOGFIRE_TOKEN,
            "LOG_LEVEL": settings.LOG_LEVEL,
            "HOSTNAME": hostname,
            "BROWSER_TIMEOUT": settings.BROWSER_TIMEOUT,
            "DEFAULT_PROXY_TYPE": settings.DEFAULT_PROXY_TYPE,
            "SENTRY_DSN": settings.CONTAINER_SENTRY_DSN,
            "DATA_DIR": dst_data_dir,
            "PORT": "80",
        }
        cap_adds = ["NET_BIND_SERVICE"]
        entrypoint = None
        cmd = None

        # If host is not macOS, container needs the tailscale router to access proxy service,
        # so we need to override the entrypoint to install iproute2 and add tailscale routing.
        # The container also needs NET_ADMIN capabilities
        if platform.system() != "Darwin":
            entrypoint = "/bin/sh"
            cmd = [
                "-c",
                f"ip route add 100.64.0.0/10 via {cls._tailscale_router_ip()} && exec /app/entrypoint.sh",
            ]
            cap_adds.append("NET_ADMIN")

        container = await client.create_or_replace_container(
            name=container_name,
            hostname=hostname,
            user="root",
            image=CONTAINER_IMAGE_NAME,
            entrypoint=entrypoint,
            envs=env,
            volumes=[
                f"{src_data_dir}:{dst_data_dir}:rw",
                f"{src_proxies_file}:{dst_proxies_file}:ro",
            ],
            labels=CONTAINER_LABELS,
            cap_adds=cap_adds,
            cmd=cmd,
        )
        logger.info(f"Created or reloaded container", container=container.dump())
        return container

    @classmethod
    def _get_mount_dirs(cls):
        return [item for item in settings.container_mount_parent_dir.iterdir() if item.is_dir()]

    @classmethod
    def _generate_container_hostname(cls) -> str:
        """Generate a random name until it is not in the existing names."""
        mount_dirs = cls._get_mount_dirs()
        existing_hostnames = set(item.stem for item in mount_dirs)
        while (hostname := generate(FRIENDLY_CHARS, 6)) in existing_hostnames:
            continue
        return hostname

    @classmethod
    async def _is_container_ready(cls, container: Container) -> bool:
        if container.status != "running":
            return False

        return datetime.now(timezone.utc) > container.started_at + timedelta(
            seconds=CONTAINER_STARTUP_SECONDS
        )

    @classmethod
    async def _write_metadata(cls, container: Container, user: AuthUser):
        metadata = ContainerMetadata(user=user)
        async with aiofiles.open(container.metadata_file, "w") as f:
            await f.write(metadata.model_dump_json())

    @classmethod
    async def read_metadata(cls, hostname: str) -> ContainerMetadata | None:
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
