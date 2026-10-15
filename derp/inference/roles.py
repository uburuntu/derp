"""Hidden model roles and their launch pins. Users never pick models."""

from enum import StrEnum

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
    max_price_prompt: float | None = None  # USD per 1M tokens
    max_price_completion: float | None = None

    @property
    def models(self) -> tuple[str, ...]:
        return (self.model, *self.fallbacks)


DEFAULT_PINS: dict[ModelRole, RolePin] = {
    ModelRole.CHAT: RolePin(model="openai/gpt-6-luna"),
    ModelRole.CHAT_SPONSORED: RolePin(model="deepseek/deepseek-v4.1-flash"),
    ModelRole.CHAT_FREE: RolePin(
        model="thinkingmachines/inkling:free",
        fallbacks=("thinkingmachines/inkling-small:free", "google/gemma-4-26b-a4b-it:free"),
        zdr=False,
    ),
    ModelRole.CHAT_FRONTIER: RolePin(model="openai/gpt-6.1-sol"),
    ModelRole.IMAGE: RolePin(
        model="google/gemini-3.1-flash-lite-image", fallbacks=("bytedance-seed/seedream-5-0-flash",)
    ),
    ModelRole.STT: RolePin(model="openai/whisper-large-v3-turbo", fallbacks=("elevenlabs/scribe-v2",)),
}
