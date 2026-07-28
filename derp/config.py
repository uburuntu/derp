"""Configuration settings using Pydantic."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from derp.catalog import InferenceProvider
from derp.execution import Feature
from derp.health import DEFAULT_RUNTIME_HEALTH_PATH

# Docs: https://docs.pydantic.dev/2.8/concepts/pydantic_settings/

RELEASE_OPENROUTER_FEATURES = frozenset(
    {
        Feature.CHAT,
        Feature.INLINE_CHAT,
        Feature.IMAGE_GENERATE,
        Feature.IMAGE_EDIT,
    }
)
DEFAULT_OPENROUTER_FEATURES = RELEASE_OPENROUTER_FEATURES


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
    public_purchases_enabled: bool = False
    artifact_store_path: Path = Path(tempfile.gettempdir()) / "derp-artifacts"
    runtime_health_path: Path = DEFAULT_RUNTIME_HEALTH_PATH
    callback_signing_secret: SecretStr | None = None

    # Google API key used by all configured models
    google_api_paid_key: SecretStr

    # OpenRouter is the default inference plane. Google remains an explicit rollback.
    openrouter_api_key: SecretStr | None = None
    openrouter_app_title: str = "Derp"
    openrouter_app_url: str | None = None
    openrouter_enabled_features: Annotated[frozenset[Feature], NoDecode] = Field(
        default_factory=lambda: DEFAULT_OPENROUTER_FEATURES,
    )
    # Logfire token
    logfire_token: SecretStr
    logfire_capture_ai_content: bool = False

    # --- Non essentials ---

    operator_ids: Annotated[frozenset[int], NoDecode] = Field(
        default_factory=frozenset,
        validation_alias=AliasChoices("OPERATOR_IDS", "ADMIN_IDS"),
    )

    @field_validator("operator_ids", mode="before")
    @classmethod
    def parse_operator_ids(cls, value: object) -> frozenset[int]:
        """Accept a JSON array or a comma-separated list of Telegram user IDs."""
        if value is None or value == "":
            return frozenset()
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return frozenset()
            value = json.loads(raw) if raw.startswith("[") else raw.split(",")
        if not isinstance(value, list | tuple | set | frozenset):
            raise ValueError("OPERATOR_IDS must be a list of Telegram user IDs")
        parsed: list[int] = []
        for item in value:
            if isinstance(item, int) and not isinstance(item, bool):
                parsed.append(item)
                continue
            if not isinstance(item, str):
                raise ValueError("OPERATOR_IDS must contain only integers")
            try:
                parsed.append(int(item.strip()))
            except ValueError as exc:
                raise ValueError("OPERATOR_IDS must contain only integers") from exc
        operator_ids = frozenset(parsed)
        if any(operator_id <= 0 for operator_id in operator_ids):
            raise ValueError("OPERATOR_IDS must contain only positive integers")
        return operator_ids

    @field_validator("openrouter_enabled_features", mode="before")
    @classmethod
    def parse_openrouter_features(cls, value: object) -> frozenset[Feature]:
        """Accept JSON arrays or comma-separated feature names."""
        if value is None or value == "":
            return frozenset()
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return frozenset()
            value = json.loads(raw) if raw.startswith("[") else raw.split(",")
        if not isinstance(value, list | tuple | set | frozenset):
            raise ValueError("OPENROUTER_ENABLED_FEATURES must be a feature list")
        try:
            return frozenset(
                item if isinstance(item, Feature) else Feature(str(item).strip())
                for item in value
            )
        except ValueError as exc:
            names = ", ".join(feature.value for feature in Feature)
            raise ValueError(
                f"OPENROUTER_ENABLED_FEATURES must contain only: {names}"
            ) from exc

    @model_validator(mode="after")
    def require_production_operator(self) -> Settings:
        """Production must have an explicit operator recovery path."""
        if self.environment == "prod" and not self.operator_ids:
            raise ValueError("OPERATOR_IDS must contain at least one ID in production")
        if self.environment == "prod" and self.openrouter_api_key is None:
            raise ValueError("OPENROUTER_API_KEY is required in production")
        if (
            self.environment == "prod"
            and self.openrouter_enabled_features != RELEASE_OPENROUTER_FEATURES
        ):
            required = ", ".join(
                sorted(feature.value for feature in RELEASE_OPENROUTER_FEATURES)
            )
            raise ValueError(
                "OPENROUTER_ENABLED_FEATURES must match the reviewed production "
                f"surface: {required}"
            )
        return self

    model_config = SettingsConfigDict(
        # `.env.prod` takes priority over `.env`
        env_file=(".env", ".env.prod"),
        env_file_encoding="utf-8",
        extra="ignore",  # Ignore extra fields from .env
        populate_by_name=True,
    )

    @property
    def bot_id(self) -> int:
        return int(self.telegram_bot_token.get_secret_value().split(":")[0])

    @property
    def callback_signing_key(self) -> bytes:
        """Derive a fixed-length key without exposing configured secret text."""
        configured = self.callback_signing_secret or self.telegram_bot_token
        return hashlib.sha256(
            b"derp:callback-signing:v1\0"
            + configured.get_secret_value().encode("utf-8")
        ).digest()

    def uses_openrouter(self, feature: Feature) -> bool:
        """Return whether a feature should use the default OpenRouter plane."""
        return feature in self.openrouter_enabled_features

    def inference_provider(self, feature: Feature) -> InferenceProvider:
        """Resolve one feature to OpenRouter or the explicit Google rollback."""
        if not isinstance(feature, Feature):
            raise TypeError("feature must be a Feature")
        return (
            InferenceProvider.OPENROUTER
            if self.uses_openrouter(feature)
            else InferenceProvider.GOOGLE
        )

    @property
    def resolved_openrouter_app_url(self) -> str:
        return self.openrouter_app_url or f"https://t.me/{self.bot_username}"


settings = Settings()
