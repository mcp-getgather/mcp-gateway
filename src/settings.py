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

    @property
    def auth_provider(self) -> str:
        """Only supports GitHub for now."""
        return "github"


settings = Settings()
