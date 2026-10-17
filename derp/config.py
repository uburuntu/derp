"""Environment configuration — the only configuration authority.

Credential fields are ``repr=False`` so they never reach logs or reprs, and are
collected by ``ops.redaction``. Optional integrations are checked when used via
``Settings.require``; importing this module needs no credentials.
"""

from functools import cache
from math import ceil
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from derp.inference.roles import DEFAULT_PINS, ModelRole, RolePin


class MissingIntegrationError(Exception):
    def __init__(self, *fields: str) -> None:
        self.fields = fields
        super().__init__("Optional integration is not configured: " + ", ".join(fields))


class Pack(BaseModel, frozen=True):
    """A one-off Derpcoin pack bought with Stars."""

    id: str
    stars: int = Field(gt=0)
    credits: int = Field(gt=0)


class Plan(BaseModel, frozen=True):
    """Derp+ — a 30-day Stars subscription granting a monthly allowance (no other perks)."""

    id: str = "derp_plus_v1"
    stars: int = Field(default=500, gt=0, le=10_000)
    credits: int = Field(default=7_150, gt=0)
    period_seconds: int = 2_592_000


class RunCaps(BaseModel, frozen=True):
    """Hard per-run limits — the abuse guard alongside the global sponsored cap and owner blocks."""

    tool_calls: int = 8
    visible_outputs: int = 3
    output_tokens: int = 2_000
    timeout_seconds: float = 120
    credits: int = 200


class ContextWindows(BaseModel, frozen=True):
    """Recent-history token budgets per funding tier; deeper context comes from tools and digests."""

    paid: int = 16_000
    sponsored: int = 8_000
    free: int = 6_000


class Retention(BaseModel, frozen=True):
    """Raw-history retention in days and digest lifetime (digests outlive raw messages)."""

    options: tuple[int, ...] = (1, 7, 30, 365)
    group_days: int = 30
    private_days: int = 365
    digest_days: int = 365


class MediaLimits(BaseModel, frozen=True):
    """Local media worker caps (CPU-bound effects on a shared VPS)."""

    workers: int = 2
    queue: int = 8
    max_input_bytes: int = 20 * 1024 * 1024
    max_output_bytes: int = 50 * 1024 * 1024
    max_pixels: int = 16_000_000
    timeout_seconds: float = 60
    voice_max_seconds: int = 600


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        hide_input_in_errors=True,
    )

    environment: Literal["dev", "prod", "test"] = "dev"

    # Telegram
    telegram_bot_token: str = Field(default="", repr=False)
    telegram_api_base: str = ""  # local Bot API server or the e2e fake; empty = api.telegram.org
    owner_ids: list[int] = Field(default_factory=list)
    ops_chat_id: int | None = None  # error digests; other owner pings go to owner DMs
    heartbeat_path: Path = Path("/tmp/derp.poll")  # noqa: S108 - container-local liveness file

    # Storage
    database_url: str = Field(default="", repr=False)
    database_pool_size: int = 10

    # Inference
    openrouter_api_key: str = Field(default="", repr=False)
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_app_url: str = "https://t.me/DerpRobot"
    openrouter_app_title: str = "Derp"
    source_url: str = "https://github.com/uburuntu/derp"
    source_revision: str = "main"  # the deployed commit, baked into the image at build
    model_pins: dict[ModelRole, RolePin] = Field(default_factory=lambda: dict(DEFAULT_PINS))
    # Families the scout may propose. No OpenAI or Anthropic chat models: too buttoned-up for Derp's voice.
    model_family_allowlist: tuple[str, ...] = (
        "google/",
        "deepseek/",
        "x-ai/",
        "moonshotai/",
        "qwen/",
        "z-ai/",
        "mistralai/",
        "thinkingmachines/",
        "bytedance-seed/",
        "black-forest-labs/",
        "elevenlabs/",
    )

    # Money — 1 Derpcoin = usd_per_credit of charged value; markup covers fees and sponsored usage
    usd_per_credit: float = 0.001
    markup: float = 1.2
    credits_per_star: int = 13
    packs: tuple[Pack, ...] = (
        Pack(id="pack_s_v1", stars=50, credits=650),
        Pack(id="pack_m_v1", stars=250, credits=3_250),
        Pack(id="pack_l_v1", stars=750, credits=9_750),
    )
    plan: Plan = Plan()
    sponsored_daily_credits: int = 10
    sponsored_global_daily_usd: float = 1.0
    # Creator-paid speech-to-text per UTC day past which ambient voice is no longer transcribed
    # (invocations and the messages they reply to still are).
    ambient_stt_daily_usd: float = 0.5
    openrouter_low_usd: float = 5.0  # owner ping when the API key's spending limit has less left
    approval_credits: int = 60  # a single action costing more asks first
    approval_balance_fraction: float = (
        0.25  # …as does one costing more than this share of the payer's balance
    )
    approval_minutes: int = 15
    image_price_usd: float = 0.045  # upper estimate of one standard image on the image pin
    stt_price_usd_per_minute: float = 0.007  # upper estimate on the STT pin, used when no cost is reported
    # Upper estimate of one web search's fee (OpenRouter's default engine, Exa, is $0.007 per search):
    # a search starts only while the run's hold still covers it.
    web_search_price_usd: float = 0.01
    effect_credits: int = (
        2  # flat fee on paid runs for heavy local effects (video memes, reversed clips, cutouts)
    )
    sponsored_effects_per_day: int = 3  # heavy local effects per person per day on sponsored runs
    background_model: Path = Path.home() / ".cache" / "derp" / "u2netp.onnx"  # fetched at image build
    run_caps: RunCaps = RunCaps()
    run_concurrency: int = 12  # agent runs in flight at once, across all chats
    context_windows: ContextWindows = ContextWindows()
    retention: Retention = Retention()
    media: MediaLimits = MediaLimits()

    # Telemetry — optional, fails soft
    logfire_token: str = Field(default="", repr=False)
    logfire_url: str = ""  # project link appended to error digests

    # Third-party tools — optional; tools disable themselves when missing
    wolfram_app_id: str = Field(default="", repr=False)
    jdoodle_client_id: str = Field(default="", repr=False)
    jdoodle_client_secret: str = Field(default="", repr=False)
    acrcloud_host: str = ""
    acrcloud_access_key: str = Field(default="", repr=False)
    acrcloud_access_secret: str = Field(default="", repr=False)

    @property
    def is_prod(self) -> bool:
        return self.environment == "prod"

    def validate_core(self) -> None:
        missing = [
            name.upper()
            for name in ("telegram_bot_token", "database_url", "openrouter_api_key")
            if not getattr(self, name)
        ]
        if missing:
            raise ValueError("Missing required settings: " + ", ".join(missing))
        if self.is_prod and not self.owner_ids:
            raise ValueError("OWNER_IDS must be set in production")

    def require(self, *names: str) -> Any:
        missing = [name for name in names if not getattr(self, name)]
        if missing:
            raise MissingIntegrationError(*missing)
        return getattr(self, names[0]) if len(names) == 1 else tuple(getattr(self, name) for name in names)

    def credits_for_usd(self, usd: float) -> int:
        """Charged Derpcoins for a provider cost: ``ceil(cost × markup / usd_per_credit)``."""
        return ceil(round(usd * self.markup / self.usd_per_credit, 9))

    def secret_values(self) -> list[str]:
        return [
            value
            for name, field in type(self).model_fields.items()
            if field.repr is False and isinstance(value := getattr(self, name), str) and value
        ]


@cache
def get_settings() -> Settings:
    return Settings()
