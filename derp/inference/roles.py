"""Hidden model roles and their launch pins. Users never pick models."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class ModelRole(StrEnum):
    CHAT = "chat"
    CHAT_SPONSORED = "chat_sponsored"
    CHAT_FREE = "chat_free"
    IMAGE = "image"
    STT = "stt"


class RolePin(BaseModel, frozen=True):
    """A role's primary model plus ordered fallbacks; ``zdr`` is sent per request."""

    model: str
    fallbacks: tuple[str, ...] = Field(default=(), max_length=2)
    zdr: bool = True
    # Reasoning effort for chat roles; banter rarely needs deep thought and reasoning tokens are billed.
    reasoning: Literal["none", "minimal", "low", "medium", "high"] | None = "low"
    vision: bool = (
        True  # every model in the pin accepts image input; otherwise images reach it as descriptions
    )
    # Prefer providers that are fast right now (soft: never blocks a request); still price-weighted among them.
    fast: bool = False
    max_price_prompt: float | None = None  # USD per 1M tokens
    max_price_completion: float | None = None
    # Provider allowlist per model for APIs without a ``zdr`` routing flag (Images API).
    only: dict[str, tuple[str, ...]] = Field(default_factory=dict[str, tuple[str, ...]])

    @property
    def models(self) -> tuple[str, ...]:
        return (self.model, *self.fallbacks)


DEFAULT_PINS: dict[ModelRole, RolePin] = {
    # A dated snapshot, so behaviour doesn't shift under us; its cache reads cost a tenth of GLM's.
    # GLM's reasoning can't be disabled, but at "low" it emits next to none.
    ModelRole.CHAT: RolePin(
        model="deepseek/deepseek-v4.1-flash-1017", fallbacks=("z-ai/glm-5.3-flash",), fast=True
    ),
    ModelRole.CHAT_SPONSORED: RolePin(
        model="deepseek/deepseek-v4.1-flash-1017", fallbacks=("z-ai/glm-5.3-flash",)
    ),
    ModelRole.CHAT_FREE: RolePin(
        model="thinkingmachines/inkling:free",
        fallbacks=("thinkingmachines/inkling-small:free", "google/gemma-4-26b-a4b-it:free"),
        zdr=False,
        vision=False,
    ),
    ModelRole.IMAGE: RolePin(
        model="google/gemini-3.1-flash-lite-image",
        fallbacks=("bytedance-seed/seedream-5-0-flash",),
        only={
            "google/gemini-3.1-flash-lite-image": ("google-vertex",),
            "bytedance-seed/seedream-5-0-flash": ("seed",),
        },
    ),
    ModelRole.STT: RolePin(model="openai/whisper-large-v3-turbo", fallbacks=("elevenlabs/scribe-v2",)),
}
