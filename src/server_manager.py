import asyncio
import platform
import random
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, cast

import aiofiles
import aiorwlock
from aiodocker import Docker
from aiodocker.containers import DockerContainer
from nanoid import generate
from pydantic import BaseModel, ConfigDict, Field

from src.auth import AuthUser
from src.logs import logger
from src.settings import settings

UNASSIGNED_USER_ID = "UNASSIGNED"
FRIENDLY_CHARS: str = "23456789abcdefghijkmnpqrstuvwxyz"
CONTAINER_STARTUP_TIME = timedelta(seconds=20)

# "internal-net" is the network name used in docker-compose.yml
# the full network name is prefixed by settings.DOCKER_PROJECT_NAME
DOCKER_NETWORK_NAME = f"{settings.DOCKER_PROJECT_NAME}_internal-net"

SERVER_IMAGE_NAME = f"{settings.DOCKER_PROJECT_NAME}_mcp-getgather"


class ContainerMetadata(BaseModel):
    user: AuthUser


class Container(BaseModel):
    """
    Wrapper around a DockerContainer.
    Container names are [USER_ID]-[HOSTNAME] or UNASSIGNED-[HOSTNAME]
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    id: str
    hostname: str
    ip: str

    container: DockerContainer = Field(exclude=True)

    @classmethod
    def from_docker(cls, container: DockerContainer) -> "Container":
        info = container._container  # type: ignore[reportPrivateUsage]
        return cls(
            id=container.id[:12],
            hostname=info["Name"].strip("/").split("-")[-1],
            ip=info["NetworkSettings"]["Networks"][DOCKER_NETWORK_NAME]["IPAddress"],
            container=container,
        )

    @property
    def info(self) -> dict[str, Any]:
        """Return data of DockerContainer.show()."""
        return self.container._container  # type: ignore[reportPrivateUsage]

    @property
    def ready(self) -> bool:
        state: dict[str, Any] = self.info["State"]
        if not state["Running"]:
            return False

        started_at = datetime.fromisoformat(state["StartedAt"].rstrip("Z")).replace(
            tzinfo=timezone.utc
        )
        return datetime.now(timezone.utc) > started_at + CONTAINER_STARTUP_TIME

    @classmethod
    def name_for_user(cls, user: AuthUser | None, hostname: str) -> str:
        return f"{user.user_id if user else UNASSIGNED_USER_ID}-{hostname}"

    @property
    def mount_dir(self) -> Path:
        return self.mount_dir_for_hostname(self.hostname)

    @classmethod
    def mount_dir_for_hostname(cls, hostname: str) -> Path:
        path = settings.server_mount_parent_dir / hostname
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def metadata_file(self) -> Path:
        return self.metadata_file_for_hostname(self.hostname)

    @classmethod
    def metadata_file_for_hostname(cls, hostname: str) -> Path:
        return cls.mount_dir_for_hostname(hostname) / "metadata.json"


class ServerManager:
    """
    Manages the lifecycle and routing of containers.

    === Containers ===
    - Containers run the SERVER_IMAGE_NAME service in the same network as the gateway.
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
    - The pool maintains a list of settings.MIN_SERVER_POOL_SIZE unassigned containers.
    - When a new user connects, the manager will assign a container from the pool to the user, and backfill the pool.
      Assignment updates the container name from UNASSIGNED-[HOSTNAME] to [USER_ID]-[HOSTNAME].
    - Containers can also be reloaded on demand, in order to update the container image.
    """

    @classmethod
    async def get_user_container(cls, user: AuthUser) -> Container:
        """Get the container of the user. Assign one if not exists."""
        container = await cls._get_container(user.user_id)
        if not container:
            async with docker_client(lock="write") as docker:
                container = await cls._assign_container(user, docker=docker)
                await cls.backfill_container_pool(docker=docker)
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
        async with docker_client(lock="write") as docker:
            if state == "stopped":
                running_containers = await cls._get_containers(docker=docker)
                running_dirs = set(container.mount_dir for container in running_containers)
                mount_dirs = [item for item in mount_dirs if item not in running_dirs]

            await asyncio.gather(
                *[
                    cls._create_or_replace_container(mount_dir=item, docker=docker)
                    for item in mount_dirs
                ],
            )
            logger.info(f"Reloaded {len(mount_dirs)} containers")

            await cls.backfill_container_pool(docker=docker)

    @classmethod
    async def backfill_container_pool(cls, *, docker: Docker | None = None):
        async with docker_client(docker, lock="write") as _docker:
            containers = await cls._get_containers(
                partial_name=UNASSIGNED_USER_ID, docker=_docker, only_ready=False
            )
            num = settings.MIN_CONTAINER_POOL_SIZE - len(containers)
            if num <= 0:
                return
            logger.info(f"Backfill server pool with {num} servers")
            await asyncio.gather(*[
                cls._create_or_replace_container(docker=_docker) for _ in range(num)
            ])

    @classmethod
    async def pull_server_image(cls):
        async with docker_client() as docker:
            source_image = "ghcr.io/mcp-getgather/mcp-getgather:latest"
            await docker.images.pull(source_image)
            await docker.images.tag(source_image, repo=SERVER_IMAGE_NAME)

    @classmethod
    async def _get_containers(
        cls,
        *,
        partial_name: str | None = None,
        docker: Docker | None = None,
        only_ready: bool = True,
    ) -> list[Container]:
        filters = {
            "label": [
                f"com.docker.compose.project={settings.DOCKER_PROJECT_NAME}",
                f"com.docker.compose.service=mcp-getgather",
            ]
        }
        if partial_name:
            filters["name"] = [partial_name]

        async with docker_client(docker, lock="read") as docker:
            containers = await docker.containers.list(filters=filters)  # type: ignore[reportUnknownMemberType]
            # load info for all containers
            await asyncio.gather(*[
                container.show()  # type: ignore[reportUnknownMemberType]
                for container in containers
            ])

        containers = [Container.from_docker(container) for container in containers]
        if only_ready:
            containers = [c for c in containers if c.ready]
        return containers

    @classmethod
    async def _get_container(
        cls, partial_name: str, *, docker: Docker | None = None
    ) -> Container | None:
        containers = await cls._get_containers(
            partial_name=partial_name, docker=docker, only_ready=False
        )
        if len(containers) > 1:
            raise ValueError(
                f"Multiple containers found for {partial_name}, use _get_containers instead"
            )
        return containers[0] if containers else None

    @classmethod
    async def _get_random_unassigned_container(cls, docker: Docker | None = None):
        containers = await cls._get_containers(partial_name=UNASSIGNED_USER_ID, docker=docker)
        if not containers:
            raise RuntimeError("No unassigned containers found")
        container = random.choice(containers)
        logger.info(f"Randomly selected unassigned container {container.id}")
        return container

    @classmethod
    def _get_mount_dirs(cls):
        return [item for item in settings.server_mount_parent_dir.iterdir() if item.is_dir()]

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
        cls, *, mount_dir: Path | None = None, docker: Docker | None = None
    ):
        # catch all exceptions in _create_or_replace_container_impl,
        # docker_client handles raise based on whether it's nested or not
        async with docker_client(docker, lock="write") as _docker:
            try:
                return await cls._create_or_replace_container_impl(_docker, mount_dir=mount_dir)
            except Exception as e:
                logger.error(
                    f"Failed to create or reload container for mount_dir: {mount_dir}: {e}"
                )
                raise e

    @classmethod
    async def _create_or_replace_container_impl(
        cls, docker: Docker, *, mount_dir: Path | None = None
    ):
        """Create a fresh container for UNASSIGNED user or load from a mount_dir for an existing user."""
        if mount_dir is None:
            hostname = cls._generate_container_name()
            user = None
        else:
            hostname = mount_dir.stem
            metadata = await cls._read_metadata(hostname)
            user = metadata.user if metadata else None

        container_name = Container.name_for_user(user, hostname)

        src_data_dir = str(Container.mount_dir_for_hostname(hostname).resolve())
        dst_data_dir = "/app/data"

        config: dict[str, Any] = {
            "Image": SERVER_IMAGE_NAME,
            "Env": [
                f"ENVIRONMENT={settings.GATEWAY_ORIGIN}",
                f"LOGFIRE_TOKEN={settings.LOGFIRE_TOKEN}",
                f"LOG_LEVEL={settings.LOG_LEVEL}",
                f"BROWSER_TIMEOUT={settings.BROWSER_TIMEOUT}",
                f"DEFAULT_PROXY_TYPE={settings.DEFAULT_PROXY_TYPE}",
                f"PROXIES_CONFIG={settings.PROXIES_CONFIG}",
                f"OPENAI_API_KEY={settings.OPENAI_API_KEY}",
                f"SENTRY_DSN={settings.SERVER_SENTRY_DSN}",
                f"DATA_DIR={dst_data_dir}",
                f"HOSTNAME={hostname}",
                "PORT=80",
            ],
            "HostConfig": {"Binds": [f"{src_data_dir}:{dst_data_dir}:rw"]},
            "NetworkingConfig": {"EndpointsConfig": {DOCKER_NETWORK_NAME: {"Aliases": [hostname]}}},
            "Labels": {
                "com.docker.compose.project": settings.DOCKER_PROJECT_NAME,
                "com.docker.compose.service": "mcp-getgather",
            },
        }

        # If host is not macOS, container needs the tailscale router to access proxy service,
        # so we need to override the entrypoint to install iproute2 and add tailscale routing.
        # The container also needs NET_ADMIN capabilities
        if platform.system() != "Darwin":
            config.update({
                "Entrypoint": ["/bin/sh", "-c"],
                "Cmd": [
                    f"ip route add 100.64.0.0/10 via {cls._tailscale_router_ip()} &&"
                    " exec /app/entrypoint.sh"
                ],
            })
            cast(dict[str, Any], config["HostConfig"]).update({"CapAdd": ["NET_ADMIN"]})

        container = await docker.containers.create_or_replace(container_name, config)
        await container.start()  # type: ignore[reportUnknownMemberType]
        logger.info(f"Created or reloaded server hostname: {hostname}, id: {container.id[:12]}")
        return hostname

    @classmethod
    async def _assign_container(cls, user: AuthUser, *, docker: Docker | None = None):
        async with docker_client(docker, lock="write") as docker:
            # rename the container to [AuthUser.user_id]-[HOSTNAME]
            container = await cls._get_random_unassigned_container(docker)
            assigned_container_name = f"{user.user_id}-{container.hostname}"
            await container.container.rename(assigned_container_name)  # type: ignore[reportUnknownMemberType]

            await cls._write_metadata(container, user)

            if platform.system() != "Darwin":
                exec = await container.container.exec(
                    ["ip", "route", "add", "100.64.0.0/10", "via", cls._tailscale_router_ip()],
                    privileged=True,
                )
                await exec.start(detach=True)

            logger.info(f"Assigned container {container.id} to {user.user_id}")

        return container

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
        return f"{settings.DOCKER_SUBNET_PREFIX}.2"

    @classmethod
    def _user_server_host(cls, user: AuthUser) -> str:
        return f"{user.user_id}.{user.auth_provider}"


CONTAINER_LOCK = aiorwlock.RWLock()


@asynccontextmanager
async def docker_client(
    client: Docker | None = None, *, lock: Literal["read", "write"] | None = None
):
    nested = client is not None
    _client = client or Docker()
    nested_exceptions: list[Exception] = []

    try:
        if not nested:  # only acquire lock if at the outer level
            if lock == "read":
                await CONTAINER_LOCK.reader_lock.acquire()
            elif lock == "write":
                await CONTAINER_LOCK.writer_lock.acquire()

        yield _client
    except Exception as e:
        # collect all exceptions in nested session, so it doesn't break others
        # and raise them together in the 'finally' block
        logger.exception(f"Docker operation failed: {e}")
        nested_exceptions.append(e)
    finally:
        if nested:
            return

        await _client.close()
        if lock == "read":
            CONTAINER_LOCK.reader_lock.release()
        elif lock == "write":
            CONTAINER_LOCK.writer_lock.release()

        if not nested_exceptions:
            return

        if len(nested_exceptions) == 1:
            raise nested_exceptions[0]
        else:
            raise ExceptionGroup(
                "Multiple exceptions occurred during docker operations", nested_exceptions
            )
