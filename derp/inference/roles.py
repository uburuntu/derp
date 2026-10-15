"""Hidden model roles and their launch pins. Users never pick models."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class ModelRole(StrEnum):
    CHAT = "chat"
    CHAT_SPONSORED = "chat_sponsored"
    CHAT_FREE = "chat_free"
    CHAT_FRONTIER = "chat_frontier"
    IMAGE = "image"
    STT = "stt"


class RolePin(BaseModel, frozen=True):
    """A role's primary model plus ordered fallbacks; ``zdr`` is sent per request."""

    model: str
    fallbacks: tuple[str, ...] = Field(default=(), max_length=2)
    zdr: bool = True
    # Reasoning effort for chat roles; banter rarely needs deep thought and reasoning tokens are billed.
    reasoning: Literal["none", "minimal", "low", "medium", "high"] | None = "low"
    max_price_prompt: float | None = None  # USD per 1M tokens
    max_price_completion: float | None = None
    # Provider allowlist per model for APIs without a ``zdr`` routing flag (Images API).
    only: dict[str, tuple[str, ...]] = Field(default_factory=dict[str, tuple[str, ...]])

    @property
    def models(self) -> tuple[str, ...]:
        return (self.model, *self.fallbacks)


DEFAULT_PINS: dict[ModelRole, RolePin] = {
    ModelRole.CHAT: RolePin(model="openai/gpt-6-luna"),
    ModelRole.CHAT_SPONSORED: RolePin(model="deepseek/deepseek-v4.1-flash", reasoning="none"),
    ModelRole.CHAT_FREE: RolePin(
        model="thinkingmachines/inkling:free",
        fallbacks=("thinkingmachines/inkling-small:free", "google/gemma-4-26b-a4b-it:free"),
        zdr=False,
    ),
    ModelRole.CHAT_FRONTIER: RolePin(model="openai/gpt-6.1-sol"),
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
