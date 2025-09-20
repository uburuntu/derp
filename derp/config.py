"""Configuration settings using Pydantic."""

import itertools
from typing import Iterable, Literal

from pydantic import Field
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
    bot_username: str = "DerpRobot"

    # Gel (ex EdgeDB) database connection string
    gel_instance: str

    gel_secret_key: str

    default_llm_model: str

    # OpenAI API key for Pydantic AI
    openai_api_key: str

    # Google API key for Pydantic AI
    google_api_key: str
    google_api_extra_keys: str
    google_api_paid_key: str

    # OpenRouter API key for Pydantic AI
    openrouter_api_key: str

    # Logfire token
    logfire_token: str

    # --- Non essentials ---

    # WebApp server config
    webapp_host: str = "127.0.0.1"
    webapp_port: int = 8081
    webapp_public_base: str | None = "https://tribe-dryer-idle-book.trycloudflare.com"

    admin_ids: set[int] = Field(
        default_factory=lambda: [
            28006241,  # @rm_bk
        ]
    )

    rmbk_id: int = 28006241

    premium_chat_ids: set[int] = Field(
        default_factory=lambda: [
            28006241,  # @rm_bk
            -1001174590460,  # Related
            -1001130715084,  # Солнышки
        ]
    )

    model_config = SettingsConfigDict(
        # `.env.prod` takes priority over `.env`
        env_file=(".env", ".env.prod"),
        env_file_encoding="utf-8",
        extra="ignore",  # Ignore extra fields from .env
    )

    @property
    def bot_id(self) -> int:
        return int(self.telegram_bot_token.split(":")[0])

    @property
    def google_api_keys(self) -> list[str]:
        return [self.google_api_key] + self.google_api_extra_keys.split(",")

    @property
    def google_api_key_iter(self) -> Iterable[str]:
        return itertools.cycle(self.google_api_keys)


settings = Settings()
