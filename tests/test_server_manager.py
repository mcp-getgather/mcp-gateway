import pytest
from assertpy import assert_that

from src.server_manager import Container, ServerManager, docker_client
from src.settings import settings


@pytest.mark.asyncio
async def test_create_new_container() -> None:
    hostname = await ServerManager._create_or_replace_container()  # type: ignore[reportPrivateUsage]

    async with docker_client() as docker:
        container_name = Container.name_for_user(None, hostname)
        container = await docker.containers.get(container_name)  # type: ignore[reportUnknownMemberType]

        info = container._container  # type: ignore[reportPrivateUsage]

    src_data_dir = Container.mount_dir_for_hostname(hostname)
    dst_data_dir = "/app/data"
    network_name = f"{settings.DOCKER_PROJECT_NAME}_{settings.DOCKER_NETWORK_NAME}"
    network_aliases = [f"{hostname}.{settings.DOCKER_DOMAIN}"]

    assert info["Name"] == f"/{container_name}"

    assert_that({
        "Image": settings.SERVER_IMAGE,
        "Labels": {"com.docker.compose.project": settings.DOCKER_PROJECT_NAME},
    }).is_subset_of(info["Config"])

    assert_that({
        "Status": "running",
        "Running": True,
    }).is_subset_of(info["State"])

    assert_that(info["Config"]["Env"]).contains(
        f"LOG_LEVEL={settings.LOG_LEVEL}",
        "BROWSER_TIMEOUT=300000",
        f"BROWSER_HTTP_PROXY={settings.BROWSER_HTTP_PROXY}",
        f"BROWSER_HTTP_PROXY_PASSWORD={settings.BROWSER_HTTP_PROXY_PASSWORD}",
        f"OPENAI_API_KEY={settings.OPENAI_API_KEY}",
        f"SENTRY_DSN={settings.SERVER_SENTRY_DSN}",
        f"DATA_DIR={dst_data_dir}",
        f"HOSTNAME={hostname}",
        "PORT=80",
    )

    assert_that(info["Mounts"]).contains({
        "Type": "bind",
        "Source": str(src_data_dir.resolve()),
        "Destination": dst_data_dir,
        "Mode": "rw",
        "RW": True,
        "Propagation": "rprivate",
    })

    assert_that(info["NetworkSettings"]["Networks"][network_name]["Aliases"]).contains(
        *network_aliases
    )
