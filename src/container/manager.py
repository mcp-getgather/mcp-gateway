import asyncio
import time
from functools import cache
from typing import Callable, Coroutine, TypeVar, cast

import psutil
from cachetools import TTLCache
from loguru import logger

from src.auth.auth import AuthUser
from src.container.container import Container
from src.container.engine import ContainerEngineClient, engine_client
from src.container.service import (
    CONTAINER_NETWORK_NAME,
    UNASSIGNED_USER_ID,
    ContainerIdentity,
    ContainerService,
)
from src.logs import log_decorator
from src.settings import settings

logger = logger.bind(topic="manager")

K = TypeVar("K")
V = TypeVar("V")

CacheCallback = Callable[[K, V], None]


class CallbackTTLCache(TTLCache[K, V]):
    """TTLCache with callbacks for expiration and pop events."""

    def __init__(
        self,
        maxsize: int,
        ttl: float,
        *,
        on_expire: CacheCallback[K, V] | None = None,
        on_pop: CacheCallback[K, V] | None = None,
        timer: Callable[[], float] = time.monotonic,
        getsizeof: Callable[[V], int] | None = None,
    ) -> None:
        super().__init__(maxsize, ttl, timer=timer, getsizeof=getsizeof)
        self.on_expire = on_expire
        self.on_pop = on_pop

    def expire(self, time: float | None = None) -> list[tuple[K, V]]:
        items = list(super().expire(time))
        if self.on_expire:
            for key, value in items:
                self.on_expire(key, value)
        return items

    def popitem(self) -> tuple[K, V]:
        key, value = super().popitem()
        if self.on_pop:
            self.on_pop(key, value)
        return key, value


CONTAINER_ACTIVE_TTL_SECONDS = 60 * 10  # 10 minutes
CONTAINER_MEMORY_BYTES = 300 * 1024 * 1024  # 300MB


@cache
def _get_total_num_containers():
    """
    Return the total number of containers that can run simultaneously.

    The number can be set by environment variable MAX_NUM_CONTAINERS.
    However, it is limited to use up to 90% of the available memory.
    """
    memory = psutil.virtual_memory()
    max_num_containers_for_memory = int(memory.total / CONTAINER_MEMORY_BYTES * 0.9)

    size = min(max_num_containers_for_memory, settings.MAX_NUM_RUNNING_CONTAINERS)
    logger.info(f"Max number of assigned containers in the active pool: {size}")
    return size


_cleanup_async_tasks = set[asyncio.Task[None]]()


def _cleanup_container(container_id: str, container: Container) -> None:
    coro = cast(Coroutine[None, None, None], ContainerManager.release_container(container))
    task: asyncio.Task[None] = asyncio.create_task(coro)
    _cleanup_async_tasks.add(task)


# Keeps track of the containers assigned to users that are running and not checkpointed.
# hostname -> Container
_active_assigned_pool = CallbackTTLCache[str, Container](
    maxsize=_get_total_num_containers(),
    ttl=CONTAINER_ACTIVE_TTL_SECONDS,
    on_expire=_cleanup_container,
    on_pop=_cleanup_container,
)


class ContainerManager:
    """
    Manages all containers that can be found in `docker/podman ps -a`, they include
    - running containers: exactly _get_assigned_container_pool_size() containers are running concurrently, including
      -- active assigned pool: containers that are assigned to users or getgather apps, running and not checkpointed
      -- standby pool: remaining containers that are not assigned to any user, names prefixed with UNASSIGNED-
    - checkpointed containers: containers that are checkpointed and ready to be restored
    - (exceptionally) error state containers: for exmaple, containers not in `docker/podman ps -a`,
      but their mount_dir exists (could be manually deleted)

    === Container Lifecyle and Routing ===
    ┌────────────────────┐     ┌───────────────┐
    │ standby unassigned │────▶│ assigned pool │◄──────────────┐
    └──────────┬─────────┘     └───────┬───────┘               │
               ▲ backfill              │ expire                │
               └───────────────────────│                       │
                                       ▼ persistent?           │
                            ┌──────────┴──────────┐            │
                            ▼ no                  ▼ yes        │
                    ┌───────┴───────┐     ┌───────┴───────┐    │
                    │    purged     │     │ checkpointed  │    │ restore, at the same time
                    └───────────────┘     └───────┬───────┘    │ remove a standby to free up resource
                                                  └────────────┘
    - All containers start as standby containers.
    - Containers are assigned to users or getgather apps as active containers. There are 2 types of assigned containers:
      -- persistent containers: containers that need to be persisted after usage, checkpointed and ready to be restored
      -- one-time containers: containers that are assigned once and then purged
    - When a new user connects, the manager will assign a standby container to the user.
      Assignment updates the container name from UNASSIGNED-[HOSTNAME] to [USER_ID]-[HOSTNAME].
      The assigned container will be added to the active pool with a TTL of CONTAINER_ACTIVE_TTL_SECONDS.
    - When an existing user reconnects, the manager will restore the container from checkpoint and add it to the active pool.
    - When a container expires from the active pool, if it is persistent, it will be checkpointed, otherwise it will be purged.
      A new standby container will be created to maintain the total number of running containers.
    - Containers can also be reloaded on demand, in order to update the container image.
    """

    @classmethod
    @log_decorator
    async def get_user_container(cls, user: AuthUser) -> Container:
        """
        Return the container assigned to the user
        - if the container is in the active pool and running, return it
        - if the container is checkpointed, restore it
        - otherwise, assign a new container to the user
        """
        container = await ContainerService.get_container(user.user_id)

        if container:
            if container.status == "running":
                if container.hostname not in _active_assigned_pool:
                    logger.warning(
                        f"Running container is not in the active pool, adding it",
                        hostname=container.hostname,
                        user=user.dump(),
                    )
            elif container.checkpointed:
                # remove a random unassigned container to free up resource so the total number of running containers is maintained
                container_to_remove = await cls.get_unassigned_container()
                await ContainerService.purge_container(container_to_remove)
                container = await ContainerService.restore_container(container)
            else:
                logger.warning(
                    f"Container is in an error state (not running or checkpointed)."
                    " A new container will be assigned.",
                    hostname=container.hostname,
                    user=user.dump(),
                )
                await ContainerService.purge_container(container)
                container = None

        if not container:
            container = await ContainerService.assign_container(user)

        # add to or refresh active pool
        _active_assigned_pool[container.hostname] = container
        return container

    @classmethod
    @log_decorator
    async def get_container_by_hostname(cls, hostname: str) -> Container:
        container = await ContainerService.get_container(hostname)
        if not container:
            raise RuntimeError(f"Container {hostname} not found")
        return container

    @classmethod
    async def get_unassigned_container(cls) -> Container:
        return await ContainerService.get_random_unassigned_container()

    @classmethod
    @log_decorator
    async def recreate_all_containers(cls):
        """
        Recreate the containers, useful for updating the image, etc.
        Note that this resets all services and terminates all client connections.
        One-time containers and unassigned containers are purged and then backfilled.
        Persistent containers are updated and checkpointed.
        """
        async with engine_client(network=CONTAINER_NETWORK_NAME, lock="write") as client:
            containers = await ContainerService.get_containers(client=client, status="all")

            logger.info(f"Reload {len(containers)} containers")
            for container in containers:
                idt = await ContainerIdentity.from_hostname(container.hostname)
                if idt.is_persistent:
                    reloaded_container = await ContainerService.create_or_replace_container(
                        mount_dir=container.mount_dir, client=client
                    )
                    await ContainerService.checkpoint_container(reloaded_container)
                else:
                    await ContainerService.purge_container(container)

            await cls.refresh_standby_pool(client=client)

    @classmethod
    @log_decorator
    async def refresh_standby_pool(cls, client: ContainerEngineClient | None = None):
        """Backfill the standby pool with new unassigned containers to maintain the total number of running containers."""
        async with engine_client(
            client=client, network=CONTAINER_NETWORK_NAME, lock="write"
        ) as _client:
            containers = await ContainerService.get_containers(client=_client, status="running")

            num = _get_total_num_containers() - len(containers)
            if num <= 0:
                return
            logger.info(f"Backfill {num} unassigned containers")

            # run sequentially to avoid overwhelming the container engine
            for _ in range(num):
                await ContainerService.create_or_replace_container(client=_client)

    @classmethod
    @log_decorator
    async def init_active_assigned_pool(cls):
        """Update the active assigned pool to match "docker/podman ps". Useful when restarting the gateway."""
        async with engine_client(network=CONTAINER_NETWORK_NAME, lock="read") as client:
            containers = await ContainerService.get_containers(client=client, status="running")

            for container in containers:
                if container.name.startswith(UNASSIGNED_USER_ID):
                    continue
                _active_assigned_pool[container.hostname] = container

    @classmethod
    @log_decorator
    async def release_container(cls, container: Container) -> None:
        """Free up resource used by a container."""
        idt = await ContainerIdentity.from_hostname(container.hostname)
        if idt.is_persistent:
            await ContainerService.checkpoint_container(container)
        else:
            await ContainerService.purge_container(container)

        # maintain the total number of running containers
        await cls.refresh_standby_pool()

    @classmethod
    @log_decorator
    async def perform_maintenance(cls):
        """
        Perform maintenance tasks on the active assigned pool.
        Return time interval for next maintenance.
        """
        # first make sure to finish previous cleanup tasks
        for task in _cleanup_async_tasks:
            await task
        _cleanup_async_tasks.clear()

        _active_assigned_pool.expire()

        return _active_assigned_pool.ttl
