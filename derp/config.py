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


class WatchLimits(BaseModel, frozen=True):
    """How much of a file one funding tier's ``inspect_media`` sends to the media pin."""

    clip_seconds: int  # videos, GIFs, video messages and video stickers, watched with sound
    audio_seconds: int  # audio files and voice messages
    pdf_pages: int


class Watching(BaseModel, frozen=True):
    """Whole-file looks on the media pin (``agent.watch``). Free models, and files past a tier's limits or the
    Bot API's download cap, get a still frame or preview picture instead.

    A look is priced before it is made from these token rates at the pin's price snapshot (these prices
    until the scout took one) and refused when the run's hold can't cover it and one more turn.
    """

    paid: WatchLimits = WatchLimits(clip_seconds=180, audio_seconds=600, pdf_pages=50)
    sponsored: WatchLimits = WatchLimits(clip_seconds=60, audio_seconds=180, pdf_pages=10)
    # Frames plus the sound track: measured about 90 on the pinned model at 720p (Gemini 2 counted ~300).
    video_tokens_per_second: int = 120
    audio_tokens_per_second: int = 32
    pdf_tokens_per_page: int = 560  # the dense end of 260–560
    prompt_tokens: int = 300  # instructions and the question
    output_tokens: int = 1_000  # the notes' cap, which the estimate assumes is used up
    timeout_seconds: float = 60
    price_usd_per_m_input: float = 0.33
    price_usd_per_m_output: float = 2.75


class Decisions(BaseModel, frozen=True):
    """Typed questions to the decision pin before a run (``agent.decisions``); any failure answers as if unasked.

    Thresholds are probabilities from the pinned model (tuned on a synthetic en/ru set); they lean towards
    running, attaching and staying at the pin's effort.
    """

    enabled: bool = True
    timeout_seconds: float = 1.5
    addressed: float = 0.3  # a name-only mention in a group runs at or above this chance it speaks to Derp
    images: float = 0.3  # fallback images are attached at or above this chance the answer needs them
    effort: float = 0.7  # a paid turn thinks harder only when this much probability says it is that hard
    price_usd_per_m_input: float = 0.042  # estimate for a call without a reported cost


class VoiceMessages(BaseModel, frozen=True):
    """Voice messages on the speech pin (``agent.tools.voice``): stock voices acting out inline audio tags."""

    max_chars: int = 400  # of one performance, audio tags included (they are billed as characters)
    # USD per 1M characters by model (list prices): the speech endpoint returns audio only, never a cost.
    # A pinned model missing here is priced at the dearest one, never as free.
    prices: dict[str, float] = Field(
        default_factory=lambda: {"elevenlabs/eleven-v4-turbo": 20.0, "elevenlabs/eleven-v4": 40.0}
    )

    def price_usd(self, model: str, characters: int) -> float:
        per_million = self.prices.get(model, max(self.prices.values(), default=40.0))
        return characters * per_million / 1_000_000


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
    waiting_minutes: int = (
        30  # an ask nobody could pay for can still be resumed (buying, free models) this long
    )
    image_price_usd: float = 0.045  # upper estimate of one standard image on the image pin
    stt_price_usd_per_minute: float = 0.007  # upper estimate on the STT pin, used when no cost is reported
    # One web search's fee (Parallel "fast", pinned in inference.openrouter: $1 per 1,000 searches):
    # a search starts only while the run's hold still covers it.
    web_search_price_usd: float = 0.001
    effect_credits: int = (
        2  # flat fee on paid runs for heavy local effects (video memes, reversed clips, cutouts)
    )
    sponsored_effects_per_day: int = 3  # heavy effects (video, cutouts, voice) per person per day, sponsored
    background_model: Path = Path.home() / ".cache" / "derp" / "u2netp.onnx"  # fetched at image build
    run_caps: RunCaps = RunCaps()
    run_concurrency: int = 12  # agent runs in flight at once, across all chats
    context_windows: ContextWindows = ContextWindows()
    retention: Retention = Retention()
    media: MediaLimits = MediaLimits()
    watching: Watching = Watching()
    decisions: Decisions = Decisions()
    speech: VoiceMessages = VoiceMessages()

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
