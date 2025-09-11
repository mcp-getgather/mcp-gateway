from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.resolve()

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_DIR / ".env", env_ignore_empty=True, extra="ignore"
    )
    ENVIRONMENT: str = "local"
    APP_NAME: str = ""
    LOG_LEVEL: str = "INFO"
    GIT_REV: str = ""