import asyncio
import json
import platform
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, NamedTuple, Protocol

import aiorwlock
from loguru import logger

from src.container.container import Container
from src.settings import settings

CONTAINER_ENGINE_LOCK = aiorwlock.RWLock()

ContainerBasicInfo = NamedTuple("ContainerBasicInfo", [("id", str), ("name", str)])


class ContainerEngineClient:
    """Wrapper for docker/podman CLI."""

    def __init__(
        self,
        engine: Literal["docker", "podman"],
        *,
        network: str,
        startup_seconds: float = 5,
        lock: Literal["read", "write"] | None = None,
    ):
        self.engine = engine
        self.network = network
        self.lock = lock
        self.socket = get_container_engine_socket(engine)
        self.startup_seconds = startup_seconds

    async def run(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        as_root: bool = False,
        timeout: float = 5,
    ) -> str:
        if platform.system() != "Darwin":
            env = env or {}
            env["DOCKER_HOST"] = self.socket
            if self.engine == "podman":
                env["CONTAINER_HOST"] = self.socket

        if self.engine == "podman":
            args = ("--remote", *args)

        return await run_cli(self.engine, *args, env=env, as_root=as_root, timeout=timeout)

    async def list_containers_basic(
        self,
        *,
        partial_name: str | None = None,
        labels: dict[str, str] | None = None,
        status: Literal["running", "all"] = "all",
    ) -> list[ContainerBasicInfo]:
        args: list[str] = []
        if status == "all":
            args.append("--all")

        filters: list[str] = []
        if partial_name:
            filters.append(f"name={partial_name}")
        if labels:
            filters.extend([f"label={k}={v}" for k, v in labels.items()])

        for filter in filters:
            args.extend(["--filter", filter])

        result = await self.run("container", "ls", *args, "--format", "{{.ID}} {{.Names}}")
        infos: list[ContainerBasicInfo] = []
        for line in result.splitlines():
            id, name = line.split(" ")
            infos.append(ContainerBasicInfo(id=id, name=name))
        return infos

    async def list_containers(
        self,
        *,
        partial_name: str | None = None,
        labels: dict[str, str] | None = None,
        status: Literal["running", "all"] = "all",
    ) -> list[Container]:
        basic_infos = await self.list_containers_basic(
            partial_name=partial_name, labels=labels, status=status
        )
        ids = [item.id for item in basic_infos]
        if not ids:
            return []
        infos = await self.inspect_containers(*ids)
        return [Container.from_inspect(info, network_name=self.network) for info in infos]

    async def get_container(self, *, id: str | None = None, name: str | None = None) -> Container:
        if id:
            info = await self.inspect_container(id)
            container = Container.from_inspect(info, network_name=self.network)
            if name and name not in container.hostname:
                raise RuntimeError(f"Container id {id} and name {name} mismatch")
            return container
        if name:
            containers = await self.list_containers(partial_name=name, status="all")
            if len(containers) > 1:
                raise RuntimeError(f"Multiple containers found for name: {name}")
            if not containers:
                raise RuntimeError(f"No container found for name: {name}")
            return containers[0]

        raise RuntimeError("Either id or name must be provided")

    async def inspect_container(self, id: str) -> dict[str, str]:
        infos = await self.inspect_containers(id)
        return infos[0]

    async def inspect_containers(self, *ids: str) -> list[dict[str, str]]:
        if not ids:
            return []

        result = await self.run("container", "inspect", *ids, "--format", "json")
        infos = json.loads(result)
        if len(infos) != len(ids):
            raise Exception(f"Failed to inspect containers: {ids}")
        return infos

    async def create_container(
        self,
        *,
        name: str,
        hostname: str,
        user: str,
        image: str,
        entrypoint: str | None = None,
        cmd: list[str] | None = None,
        envs: dict[str, Any] | None = None,
        volumes: list[str] | None = None,
        labels: dict[str, str] | None = None,
        cap_adds: list[str] | None = None,
    ):
        args = ["run", "-d", "--restart", "on-failure:3"]
        args.extend(["--name", name])
        args.extend(["--hostname", hostname])
        args.extend(["--user", user])
        # Add DNS servers for external name resolution
        args.extend(["--dns", "8.8.8.8"])
        args.extend(["--dns", "1.1.1.1"])
        if envs:
            for key, value in envs.items():
                args.extend(["--env", f"{key}={value}"])
        if volumes:
            for volume in volumes:
                args.extend(["--volume", volume])
        if labels:
            for key, value in labels.items():
                args.extend(["--label", f"{key}={value}"])
        if cap_adds:
            for cap in cap_adds:
                args.extend(["--cap-add", cap])
        args.extend(["--network", self.network])
        if entrypoint:
            args.extend(["--entrypoint", entrypoint])
        args.append(image)
        if cmd:
            args.extend(cmd)

        # Use longer timeout for container creation (especially on Docker Desktop for macOS)
        id = await self.run(*args, timeout=30)
        info = await self.inspect_container(id)
        return Container.from_inspect(info, network_name=self.network)

    async def create_or_replace_container(
        self,
        *,
        name: str,
        hostname: str,
        user: str,
        image: str,
        entrypoint: str | None = None,
        cmd: list[str] | None = None,
        envs: dict[str, Any] | None = None,
        volumes: list[str] | None = None,
        labels: dict[str, str] | None = None,
        cap_adds: list[str] | None = None,
    ):
        containers = await self.list_containers(partial_name=name, status="all")
        if len(containers) > 1:
            raise Exception(f"Replace failed: multiple containers found for name: {name}")

        if containers:
            await self.delete_container(containers[0].id)

        return await self.create_container(
            name=name,
            hostname=hostname,
            user=user,
            image=image,
            entrypoint=entrypoint,
            cmd=cmd,
            envs=envs,
            volumes=volumes,
            labels=labels,
            cap_adds=cap_adds,
        )

    async def start_container(self, id: str):
        await self.run("container", "start", id)

    async def checkpoint_container(self, id: str):
        if self.engine == "podman" and platform.system() != "Darwin":
            await self.run("container", "checkpoint", id, as_root=True)
        else:
            raise RuntimeError("Checkpoint is only supported for podman on Linux")

    async def restore_container(self, id: str):
        if self.engine == "podman" and platform.system() != "Darwin":
            await self.run("container", "restore", id, as_root=True)
        else:
            raise RuntimeError("Restore is only supported for podman on Linux")

    async def connect_network(self, network_name: str, id: str):
        try:
            await self.run("network", "connect", network_name, id)
        except Exception as e:
            container = await self.get_container(id=id)
            if not container.ip:
                raise e
            else:
                logger.warning(
                    "Error connecting container to network, but container already has an IP address, skipping",
                    container=container.dump(),
                    network=network_name,
                )

    async def disconnect_network(self, network_name: str, id: str):
        try:
            await self.run("network", "disconnect", network_name, id)
        except Exception as e:
            container = await self.get_container(id=id)
            if container.ip:
                raise e
            else:
                logger.warning(
                    "Error disconnecting container from network, but container has no IP address, skipping",
                    container=container.dump(),
                    network=network_name,
                )

    async def delete_container(self, id: str):
        await self.delete_containers(id)

    async def delete_containers(self, *ids: str):
        if not ids:
            return

        args = ["container", "rm", "--force"]
        if self.engine == "podman":
            args.extend(["--time", "0"])
        await self.run(*args, *ids)

    async def rename_container(self, id: str, new_name: str):
        await self.run("container", "rename", id, new_name)

    async def pull_image(self, image: str, *, tag: str | None = None):
        await self.run("image", "pull", image, timeout=60 * 3)
        if tag:
            await self.run("image", "tag", image, tag)

    async def delete_image(self, image: str):
        await self.run("image", "rm", "--force", image)

    async def exec(self, id: str, cmd: str, *args: str, env: dict[str, str] | None = None):
        await self.run("exec", "-d", id, cmd, *args, env=env)


@asynccontextmanager
async def engine_client(
    client: ContainerEngineClient | None = None,
    *,
    network: str | None = None,
    lock: Literal["read", "write"] | None = None,
):
    """Synchronize container engine operations."""
    nested = client is not None

    if nested:
        if client.lock is None and lock is not None:
            raise RuntimeError(
                "Cannot acquire lock in nested context. Lock must be acquired at the outer level."
            )
        if client.lock == "read" and lock == "write":
            raise RuntimeError(
                "Cannot upgrade read lock to write lock in nested context. "
                "Write lock must be acquired at the outer level."
            )
        if network:
            if client.network != network:
                raise RuntimeError("Cannot change network in nested context")
        _client = client
    else:
        if network is None:
            raise RuntimeError("Network is required when creating a new container engine client")
        _client = ContainerEngineClient(settings.CONTAINER_ENGINE, network=network, lock=lock)

    exceptions: list[Exception] = []

    try:
        if not nested:  # only acquire lock if at the outer level
            if lock == "read":
                await CONTAINER_ENGINE_LOCK.reader_lock.acquire()
            elif lock == "write":
                await CONTAINER_ENGINE_LOCK.writer_lock.acquire()

        yield _client
    except Exception as e:
        # collect all exceptions in nested session, so it doesn't break others
        # and raise them together in the 'finally' block
        logger.exception(f"Container engine operation failed: {e}")
        exceptions.append(e)
    finally:
        if nested:
            return

        if lock == "read":
            CONTAINER_ENGINE_LOCK.reader_lock.release()
        elif lock == "write":
            CONTAINER_ENGINE_LOCK.writer_lock.release()

        if not exceptions:
            return

        if len(exceptions) == 1:
            raise exceptions[0]
        else:
            raise ExceptionGroup(
                "Multiple exceptions occurred during container engine operations", exceptions
            )


def get_container_engine_socket(engine: Literal["docker", "podman"]):
    if engine == "docker":
        path = (
            Path("~/.docker/run/docker.sock").expanduser()
            if platform.system() == "Darwin"
            else "/var/run/docker.sock"
        )
    elif engine == "podman":
        path = (
            Path("~/.local/share/containers/podman/machine/podman.sock").expanduser()
            if platform.system() == "Darwin"
            else "/run/podman/podman.sock"
        )

    return f"unix://{path}"


class CLIOnError(Protocol):
    def __call__(self, returncode: int | None, error: str) -> tuple[int, str]:
        """
        Handle errors from CLI, return updated return code and error message.
        e.g., suppress expected errors by returning (0, updated error message).
        """
        ...


async def run_cli(
    cmd: str,
    *args: str,
    env: dict[str, str] | None = None,
    as_root: bool = False,
    timeout: float = 5,
    on_error: CLIOnError | None = None,
) -> str:
    """
    Run a command asynchronously and return the stdout.
    Use timeout to limit the command execution time.
    Use on_error to handle errors.
    """
    cmds = ["sudo", cmd] if as_root else [cmd]
    process = await asyncio.create_subprocess_exec(
        *cmds, *args, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    cmd_str = f"{cmd} {' '.join(args)}"
    cmd_msg = f"Command: {cmd_str}"
    if env:
        cmd_msg += f"\nEnv: {json.dumps(env)}"

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise Exception(f"CLI timed out after {timeout} seconds\n{cmd_msg}")

    returncode = process.returncode
    error = stderr.decode().strip() if stderr else ""

    if on_error:
        returncode, error = on_error(returncode, error)

    logger.debug("Executed CLI command", return_code=returncode, command=cmd_str, env=env)

    if returncode != 0:
        error_msg = f": {error}" if error else f" (returncode: {returncode})"
        raise Exception(f"CLI failed{error_msg}\n{cmd_msg}")

    return stdout.decode().strip()
