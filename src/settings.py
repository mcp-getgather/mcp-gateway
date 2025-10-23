from os import environ
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_DIR = Path(__file__).parent.parent.resolve()
FRONTEND_DIR = PROJECT_DIR / "frontend"

ENV_FILE = environ.get("ENV_FILE", PROJECT_DIR / ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_ignore_empty=True, extra="ignore")

    ENVIRONMENT: str = "local"
    LOG_LEVEL: str = "INFO"
    GIT_REV: str = "main"
    LOGFIRE_TOKEN: str = ""
    SEGMENT_WRITE_KEY: str = ""

    HOST_DATA_DIR: str = ""

    ADMIN_API_TOKEN: str = ""
    GATEWAY_ORIGIN: str = ""
    GATEWAY_SENTRY_DSN: str = ""

    OAUTH_GITHUB_CLIENT_ID: str = ""
    OAUTH_GITHUB_CLIENT_SECRET: str = ""
    OAUTH_GOOGLE_CLIENT_ID: str = ""
    OAUTH_GOOGLE_CLIENT_SECRET: str = ""

    PROXY_TIMEOUT: float = 10.0  # timeout for general operations
    PROXY_READ_TIMEOUT: float = 60 * 5  # long timeout for read operations

    DOCKER_PROJECT_NAME: str = ""
    DOCKER_NETWORK_NAME: str = ""
    DOCKER_SUBNET_PREFIX: str = ""
    DOCKER_DOMAIN: str = ""

    BROWSER_HTTP_PROXY: str = ""
    BROWSER_HTTP_PROXY_PASSWORD: str = ""

    SERVER_IMAGE: str = ""
    SERVER_SENTRY_DSN: str = ""

    MIN_CONTAINER_POOL_SIZE: int = 5

    OPENAI_API_KEY: str = ""

    @model_validator(mode="after")
    def validate_settings(self):
        required = [
            "HOST_DATA_DIR",
            "GATEWAY_ORIGIN",
            "OAUTH_GITHUB_CLIENT_ID",
            "OAUTH_GITHUB_CLIENT_SECRET",
            "DOCKER_PROJECT_NAME",
            "DOCKER_NETWORK_NAME",
            "SERVER_IMAGE",
        ]
        for name in required:
            if not getattr(self, name):
                raise ValueError(f"Missing required setting: {name}")
        return self

    @property
    def server_mount_parent_dir(self) -> Path:
        path = Path(self.HOST_DATA_DIR).expanduser() / "server_mounts"
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
        return path


settings = Settings()
