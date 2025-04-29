"""Configuration settings using Pydantic."""

from typing import Literal

from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict

# Docs: https://docs.pydantic.dev/2.8/concepts/pydantic_settings/


class Settings(BaseSettings):
    # App name used in logs
    app_name: str = "derp"

    # Allows to detect type of deployment
    environment: Literal["dev", "prod"]

    # Allows to detect environment
    is_docker: bool = False

    # Token got from https://t.me/BotFather
    telegram_bot_token: str

    # Chat to forward runtime exceptions
    events_chat_id: int | None = None

    # URL to trigger every minute to detect app's crashes
    health_check_url: HttpUrl | None = None

    # --- Non essentials ---

    admin_ids: set[int] = Field(
        default_factory=lambda: [
            28006241,  # @rm_bk
        ]
    )

    mechmath_chat_id: int = -1001091546301
    rmbk_id: int = 28006241

    surprise_gif: str = "https://t.me/mechmath/743455"

    model_config = SettingsConfigDict(
        # `.env.prod` takes priority over `.env`
        env_file=(".env", ".env.prod"),
        env_file_encoding="utf-8",
        extra="ignore",  # Ignore extra fields from .env
    )


settings = Settings()
