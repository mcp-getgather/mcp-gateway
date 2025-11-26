import asyncio
import time
from typing import Callable, Coroutine, TypeVar, cast

import psutil
from cachetools import TTLCache
from loguru import logger

from src.auth.auth import AuthUser
from src.container.container import Container
from src.container.engine import engine_client
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


CONTAINER_ACTIVE_TTL_SECONDS = 60 * 20  # 20 minutes
CONTAINER_MEMORY_BYTES = 300 * 1024 * 1024  # 300MB


def _get_assigned_container_pool_size():
    """
    Calculate the maximum number of assigned containers that can
    run simultaneously using 90% of the available memory.
    """
    memory = psutil.virtual_memory()
    max_num_containers = int(memory.total / CONTAINER_MEMORY_BYTES * 0.9)
    size = max_num_containers - settings.NUM_STANDBY_CONTAINERS
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
    maxsize=_get_assigned_container_pool_size(),
    ttl=CONTAINER_ACTIVE_TTL_SECONDS,
    on_expire=_cleanup_container,
    on_pop=_cleanup_container,
)


class ContainerManager:
    """
    Manages all containers in the following categories:
    - all containers: all containers that can be found in `docker/podman ps -a`
    - running containers
      -- standby pool: settings.NUM_STANDBY_CONTAINERS containers that are not assigned to any user
      -- active assigned pool: containers that are assigned to users, running and not checkpointed
    - checkpointed containers: containers that are assigned to non-getgather users, checkpointed
    - expired containers: containers that are assigned to getgather apps, expired from the active pool.
      They will be purged instead of checkpointed
    - error state containers: for exmaple, containers not in `docker/podman ps -a`,
      but their mount_dir exists (could be manually deleted)
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
            await cls.refresh_standby_pool()

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
    async def update_containers(cls):
        """Recreate the container to update the image. Keep the same status (running, checkpointed, etc.) after update."""
        async with engine_client(network=CONTAINER_NETWORK_NAME, lock="write") as client:
            containers = await ContainerService.get_containers(client=client)

            # reorder containers to put running ones at the beginning
            start = 0
            end = settings.NUM_STANDBY_CONTAINERS - 1
            while start < end:
                if containers[start].status == "running":
                    start += 1
                elif containers[end].status == "exited":
                    end -= 1
                else:
                    containers[start], containers[end] = containers[end], containers[start]
                    start += 1
                    end -= 1

            logger.info(f"Reload {len(containers)} containers")
            for container in containers:
                idt = await ContainerIdentity.from_hostname(container.hostname)
                if idt.is_assigned_to_getgather_app:
                    # no need to update container for getgather apps
                    await ContainerService.purge_container(container)
                    continue

                keep_running = container.status == "running"

                reloaded_container = await ContainerService.create_or_replace_container(
                    mount_dir=container.mount_dir, client=client
                )
                if idt.is_assigned_to_authenticated_user:
                    if keep_running:
                        _active_assigned_pool[reloaded_container.hostname] = reloaded_container
                    else:
                        await ContainerService.checkpoint_container(reloaded_container)
                # else: keep UNASSIGNED container running regardless of its previous status

    @classmethod
    @log_decorator
    async def refresh_standby_pool(cls):
        async with engine_client(network=CONTAINER_NETWORK_NAME, lock="write") as client:
            containers = await ContainerService.get_containers(
                partial_name=UNASSIGNED_USER_ID, client=client, only_ready=False
            )

            for container in containers:
                if container.status == "exited":
                    # UNASSIGNED containers should be always running
                    await client.start_container(container.id)

            num = settings.NUM_STANDBY_CONTAINERS - len(containers)
            if num <= 0:
                return
            logger.info(f"Backfill container pool with {num} containers")

            # run sequentially to avoid overwhelming the container engine
            for _ in range(num):
                await ContainerService.create_or_replace_container(client=client)

    @classmethod
    @log_decorator
    async def release_container(cls, container: Container) -> None:
        """Free up resource used by a container."""
        idt = await ContainerIdentity.from_hostname(container.hostname)
        if idt.is_assigned_to_authenticated_user:
            await ContainerService.checkpoint_container(container)
            _active_assigned_pool.pop(container.hostname, None)
        else:
            await ContainerService.purge_container(container)

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
