import platform
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import aiorwlock
from aiodocker import Docker
from aiodocker.containers import DockerContainer

from src.logs import logger
from src.settings import settings

CONTAINER_LOCK = aiorwlock.RWLock()


@asynccontextmanager
async def docker_client(
    client: Docker | None = None, *, lock: Literal["read", "write"] | None = None
):
    nested = client is not None
    _client = client or Docker(url=get_docker_socket())
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


async def delete_container(container: DockerContainer, *, client: Docker | None = None):
    """Handle container deletion defensively to ignore expected errors."""
    id = container.id[:12]
    async with docker_client(client, lock="write") as docker:
        try:
            await container.delete(force=True, timeout=0)  # type: ignore[reportUnknownMemberType]
        except Exception as e:
            try:
                await docker.containers.get(id)  # type: ignore[reportUnknownMemberType]
                raise e  # container still exists, raise the original error
            except Exception as e:
                logger.warning(f"Expected error deleting container {container.id}: {e}")


def get_docker_socket():
    if settings.CONTAINER_ENGINE == "docker":
        path = (
            Path("~/.docker/run/docker.sock").expanduser()
            if platform.system() == "Darwin"
            else "/var/run/docker.sock"
        )
    else:
        path = (
            Path("~/.local/share/containers/podman/machine/podman.sock").expanduser()
            if platform.system() == "Darwin"
            else "/run/podman/podman.sock"
        )

    return f"unix://{path}"
