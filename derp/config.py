"""Configuration settings using Pydantic."""

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Docs: https://docs.pydantic.dev/2.8/concepts/pydantic_settings/


class Settings(BaseSettings):
    # App name used in logs
    app_name: str = "derp"

    # Allows to detect type of deployment
    environment: Literal["dev", "prod"]

    # Token got from https://t.me/BotFather
    telegram_bot_token: SecretStr
    bot_username: str = "DerpRobot"

    # PostgreSQL database connection string
    database_url: str = "postgresql+asyncpg://localhost:5432/derp"
    polling_concurrency: int = Field(default=10, ge=1)

    # Google API key used by all configured models
    google_api_paid_key: SecretStr

    # Logfire token
    logfire_token: SecretStr
    logfire_capture_ai_content: bool = False

    # --- Non essentials ---

    admin_ids: set[int] = Field(
        default_factory=lambda: [
            28006241,  # @rm_bk
        ]
    )

    rmbk_id: int = 28006241

    model_config = SettingsConfigDict(
        # `.env.prod` takes priority over `.env`
        env_file=(".env", ".env.prod"),
        env_file_encoding="utf-8",
        extra="ignore",  # Ignore extra fields from .env
    )

    @property
    def bot_id(self) -> int:
        return int(self.telegram_bot_token.get_secret_value().split(":")[0])


settings = Settings()
