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

    SERVER_CONFIG_PATH: str = "data/servers.json"

    PROXY_TIMEOUT: float = 10.0  # timeout for general operations
    PROXY_READ_TIMEOUT: float = 120.0  # long timeout for read operations

    @property
    def auth_provider(self) -> str:
        """Only supports GitHub for now."""
        return "github"


settings = Settings()
