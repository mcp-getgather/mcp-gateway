from os import environ
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_DIR = Path(__file__).parent.parent.resolve()
FRONTEND_DIR = PROJECT_DIR / "frontend"

ENV_FILE = environ.get("ENV_FILE", PROJECT_DIR / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_ignore_empty=True, extra="ignore")

    CONTAINER_ENGINE: Literal["docker", "podman"] = "docker"

    ENVIRONMENT: str = "local"
    LOG_LEVEL: str = "INFO"
    VERBOSE: bool = False
    GIT_REV: str = "main"
    LOGFIRE_TOKEN: str = ""
    SEGMENT_WRITE_KEY: str = ""

    DATA_DIR: str = ""

    ADMIN_API_TOKEN: str = ""
    ADMIN_EMAIL_DOMAIN: str = ""
    GATEWAY_ORIGIN: str = ""
    GATEWAY_SENTRY_DSN: str = ""

    OAUTH_GITHUB_CLIENT_ID: str = ""
    OAUTH_GITHUB_CLIENT_SECRET: str = ""
    OAUTH_GOOGLE_CLIENT_ID: str = ""
    OAUTH_GOOGLE_CLIENT_SECRET: str = ""

    GETGATHER_APPS: dict[str, str] = dict()  # app key -> app name

    PROXY_TIMEOUT: float = 10.0  # timeout for general operations
    PROXY_READ_TIMEOUT: float = 60 * 5  # long timeout for read operations

    CONTAINER_PROJECT_NAME: str = ""
    CONTAINER_SUBNET_PREFIX: str = ""

    BROWSER_TIMEOUT: int = 30_000
    DEFAULT_PROXY_TYPE: str = ""
    PROXIES_CONFIG: str = ""

    CONTAINER_SENTRY_DSN: str = ""

    MAX_NUM_RUNNING_CONTAINERS: int = 5

    # for testing only
    TEST_GITHUB_OAUTH_TOKEN: str = ""

    @model_validator(mode="after")
    def validate_settings(self):
        required = ["DATA_DIR", "GATEWAY_ORIGIN", "CONTAINER_PROJECT_NAME"]
        for name in required:
            if not getattr(self, name):
                raise ValueError(f"Missing required setting: {name}")
        return self

    @property
    def container_mount_parent_dir(self) -> Path:
        path = Path(self.DATA_DIR).expanduser() / "container_mounts"
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def cleanup_container_mount_parent_dir(self) -> Path:
        """Directory to store mount directories that are cleaned up."""
        path = self.container_mount_parent_dir / "__cleanup"
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def logs_dir(self) -> Path:
        path = Path(self.DATA_DIR).expanduser() / "logs"
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        return path


settings = Settings()
