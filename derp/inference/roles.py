"""Hidden model roles and their launch pins. Users never pick models."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

type Effort = Literal["none", "minimal", "low", "medium", "high"]
EFFORTS: tuple[Effort, ...] = ("none", "minimal", "low", "medium", "high")  # shallow to deep


class ModelRole(StrEnum):
    CHAT = "chat"
    CHAT_SPONSORED = "chat_sponsored"
    CHAT_FREE = "chat_free"
    IMAGE = "image"
    STT = "stt"
    DECISION = "decision"
    MEDIA = "media"


class RolePin(BaseModel, frozen=True):
    """A role's primary model plus ordered fallbacks; ``zdr`` is sent per request."""

    model: str
    fallbacks: tuple[str, ...] = Field(default=(), max_length=2)
    zdr: bool = True
    # Reasoning effort for chat roles; banter rarely needs deep thought and reasoning tokens are billed.
    reasoning: Effort | None = "low"
    vision: bool = (
        True  # every model in the pin accepts image input; otherwise images reach it as descriptions
    )
    # Prefer providers that are fast right now (soft: never blocks a request); still price-weighted among them.
    fast: bool = False
    max_price_prompt: float | None = None  # USD per 1M tokens
    max_price_completion: float | None = None
    # The max_price came from the primary's price snapshot (``pins.with_ceiling``): routing only, never stored.
    derived_ceiling: bool = Field(default=False, exclude=True)
    # Provider allowlist per model: the Images API's only routing (it has no ``zdr`` flag); on chat completions
    # it narrows the ZDR routes further (``openrouter.provider_config``).
    only: dict[str, tuple[str, ...]] = Field(default_factory=dict[str, tuple[str, ...]])

    @property
    def models(self) -> tuple[str, ...]:
        return (self.model, *self.fallbacks)

    def rotated(self) -> RolePin:
        """The same pin led by its first fallback (the primary goes last): a second try that starts elsewhere.

        A ceiling derived from the primary's price goes: it would skip the very fallbacks the retry is for.
        """
        if not self.fallbacks:
            return self
        update: dict[str, object] = {
            "model": self.fallbacks[0],
            "fallbacks": (*self.fallbacks[1:], self.model),
        }
        if self.derived_ceiling:
            update |= {"max_price_prompt": None, "max_price_completion": None, "derived_ceiling": False}
        return self.model_copy(update=update)


DEFAULT_PINS: dict[ModelRole, RolePin] = {
    # A dated snapshot, so behaviour doesn't shift under us; its cache reads cost a tenth of GLM's.
    # GLM's reasoning can't be disabled, but at "low" it emits next to none.
    ModelRole.CHAT: RolePin(
        model="deepseek/deepseek-v4.1-flash-1017", fallbacks=("z-ai/glm-5.3-flash",), fast=True
    ),
    ModelRole.CHAT_SPONSORED: RolePin(
        model="deepseek/deepseek-v4.1-flash-1017", fallbacks=("z-ai/glm-5.3-flash",)
    ),
    # Free models are rate-limited per model and upstream provider: the chain spans providers, and
    # OpenRouter's free router (any free model that supports the request's tools) is the last rung.
    ModelRole.CHAT_FREE: RolePin(
        model="thinkingmachines/inkling:free",
        fallbacks=("google/gemma-4-26b-a4b-it:free", "openrouter/free"),
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
    # Typed questions answered with probabilities, no text (``agent.decisions``). A versioned id, not
    # ``-latest``: the thresholds in ``settings.decisions`` were tuned against it; a new model needs re-tuning.
    ModelRole.DECISION: RolePin(model="typesafe/jev-1.14", reasoning=None, vision=False),
    # Whole clips with sound, audio and PDFs for ``inspect_media`` (``agent.watch``). Vertex only: its routes are
    # this model's ZDR ones (Google AI Studio retains prompts); the base slug leaves out the flex/priority tiers.
    ModelRole.MEDIA: RolePin(
        model="google/gemini-3.5-flash-lite",
        reasoning="minimal",
        only={"google/gemini-3.5-flash-lite": ("google-vertex",)},
    ),
}
