from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_DIR = Path(__file__).parent.parent.resolve()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_DIR / ".env", env_ignore_empty=True, extra="ignore"
    )
    LOG_LEVEL: str = "INFO"

    GATEWAY_ORIGIN: str = ""
    SERVER_HOST_TEMPLATE: str = "$name"

    OAUTH_GITHUB_CLIENT_ID: str = ""
    OAUTH_GITHUB_CLIENT_SECRET: str = ""
    OAUTH_GITHUB_REDIRECT_PATH: str = "/auth/github/callback"

    PROXY_TIMEOUT: float = 10.0  # timeout for general operations
    PROXY_READ_TIMEOUT: float = 120.0  # long timeout for read operations
    # USER SERVER SETTINGS
    FLY_TOKEN: str = ""
    FLY_MACHINES_API: str = "https://api.machines.dev"
    FLY_GRAPHQL_API: str = "https://api.fly.io/graphql"
    FLY_ORG: str = ""
    FLY_REGION: str = "sjc"  # TODO: make it configurable based on user location
    FLY_IMAGE: str = "ghcr.io/mcp-getgather/mcp-getgather:latest"
    FLY_VOLUME_NAME: str = "mcp_data"
    FLY_MOUNT_PATH: str = "/app/data"
    FLY_INTERNAL_PORT: int = 23456

    @property
    def auth_provider(self) -> str:
        """Only supports GitHub for now."""
        return "github"


settings = Settings()
