"""Provider-neutral text-to-speech requests and bounded execution service."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from derp.catalog import AudioPricing, GoogleModelKey
from derp.execution import (
    ExecutionPlan,
    Failed,
    FailureReason,
    Feature,
    Outcome,
    Rejected,
    RejectionReason,
    Succeeded,
)

if TYPE_CHECKING:
    from derp.delivery.types import DeliveryMedia

MAX_TTS_TEXT_CHARS = 8_192
MAX_TTS_OUTPUT_SECONDS = 30
MAX_TTS_OUTPUT_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class TtsRequest:
    """Validated speech request with an explicit billable duration ceiling."""

    text: str
    max_output_seconds: int

    def __post_init__(self) -> None:
        normalized = self.text.strip()
        if not normalized:
            raise ValueError("TTS text must not be blank")
        if len(normalized) > MAX_TTS_TEXT_CHARS:
            raise ValueError(
                f"TTS text must be at most {MAX_TTS_TEXT_CHARS} characters"
            )
        if isinstance(self.max_output_seconds, bool) or not isinstance(
            self.max_output_seconds, int
        ):
            raise TypeError("max_output_seconds must be an integer")
        if not 1 <= self.max_output_seconds <= MAX_TTS_OUTPUT_SECONDS:
            raise ValueError(
                f"max_output_seconds must be between 1 and {MAX_TTS_OUTPUT_SECONDS}"
            )
        object.__setattr__(self, "text", normalized)


@dataclass(frozen=True, slots=True)
class TtsProviderOutput:
    """Converted provider speech plus duration needed for policy enforcement."""

    media: DeliveryMedia
    duration_seconds: float

    def __post_init__(self) -> None:
        from derp.delivery.types import DeliveryMedia, TelegramMediaKind

        if not isinstance(self.media, DeliveryMedia):
            raise TypeError("TTS provider media must be DeliveryMedia")
        if self.media.kind is not TelegramMediaKind.VOICE:
            raise ValueError("TTS provider media must be a Telegram voice")
        if self.media.mime_type != "audio/ogg":
            raise ValueError("TTS provider media must be OGG/Opus audio")
        if (
            isinstance(self.duration_seconds, bool)
            or not isinstance(self.duration_seconds, (int, float))
            or not math.isfinite(self.duration_seconds)
            or self.duration_seconds <= 0
        ):
            raise ValueError("TTS duration must be finite and positive")
        object.__setattr__(self, "duration_seconds", float(self.duration_seconds))


@dataclass(frozen=True, slots=True)
class TtsExecutionPolicy:
    """Resource and deadline limits enforced around every TTS provider."""

    max_output_seconds: int = MAX_TTS_OUTPUT_SECONDS
    max_output_bytes: int = MAX_TTS_OUTPUT_BYTES
    provider_deadline_seconds: float = 60.0

    def __post_init__(self) -> None:
        for name in ("max_output_seconds", "max_output_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_output_seconds > MAX_TTS_OUTPUT_SECONDS:
            raise ValueError(
                f"max_output_seconds must not exceed {MAX_TTS_OUTPUT_SECONDS}"
            )
        if self.max_output_bytes > MAX_TTS_OUTPUT_BYTES:
            raise ValueError(f"max_output_bytes must not exceed {MAX_TTS_OUTPUT_BYTES}")
        if (
            isinstance(self.provider_deadline_seconds, bool)
            or not isinstance(self.provider_deadline_seconds, (int, float))
            or not math.isfinite(self.provider_deadline_seconds)
            or self.provider_deadline_seconds <= 0
        ):
            raise ValueError("provider_deadline_seconds must be finite and positive")


class TtsProviderExecutor(Protocol):
    """Provider adapter with no Telegram, database, or billing effects."""

    async def synthesize(
        self,
        plan: ExecutionPlan,
        request: TtsRequest,
    ) -> Outcome[TtsProviderOutput]:
        """Synthesize and convert speech using the exact execution plan."""
        ...


class TtsFeatureService:
    """Validate TTS policy and expose delivery-ready voice media."""

    def __init__(
        self,
        executor: TtsProviderExecutor,
        *,
        policy: TtsExecutionPolicy | None = None,
    ) -> None:
        self._executor = executor
        self._policy = policy or TtsExecutionPolicy()

    async def synthesize(
        self,
        plan: ExecutionPlan,
        request: TtsRequest,
    ) -> Outcome[DeliveryMedia]:
        """Synthesize one voice within the declared and catalog limits."""
        pricing = require_tts_pricing(plan)
        output_tokens = pricing.audio_tokens_per_second * request.max_output_seconds
        if request.max_output_seconds > self._policy.max_output_seconds:
            return Rejected(RejectionReason.INVALID_INPUT)
        if (
            plan.model.output_token_limit is not None
            and output_tokens > plan.model.output_token_limit
        ):
            return Rejected(RejectionReason.INVALID_INPUT)
        return await self._run_provider(
            self._executor.synthesize(plan, request),
            request=request,
        )

    async def _run_provider(
        self,
        execution: Awaitable[Outcome[TtsProviderOutput]],
        *,
        request: TtsRequest,
    ) -> Outcome[DeliveryMedia]:
        try:
            async with asyncio.timeout(self._policy.provider_deadline_seconds):
                outcome = await execution
        except TimeoutError:
            return Failed(FailureReason.PROVIDER_ERROR)
        except Exception:
            return Failed(FailureReason.PROVIDER_ERROR)
        if isinstance(outcome, (Rejected, Failed)):
            return outcome
        if not isinstance(outcome, Succeeded):
            return Failed(FailureReason.PROVIDER_ERROR)
        if not isinstance(outcome.value, TtsProviderOutput):
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)

        output = outcome.value
        if output.duration_seconds > min(
            request.max_output_seconds,
            self._policy.max_output_seconds,
        ):
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)
        if len(output.media.data) > self._policy.max_output_bytes:
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)
        return Succeeded(output.media)


def require_tts_pricing(plan: ExecutionPlan) -> AudioPricing:
    """Return pricing only for the one canonical catalog TTS execution plan."""
    if plan.feature is not Feature.TTS:
        raise ValueError(f"{plan.feature.value} cannot execute {Feature.TTS.value}")
    if plan.model.key is not GoogleModelKey.TTS:
        raise ValueError(f"{plan.model.key.value} is not the catalog TTS model")
    if not isinstance(plan.model.pricing, AudioPricing):
        raise ValueError(f"{plan.model.key.value} does not use audio pricing")
    return plan.model.pricing


__all__ = [
    "MAX_TTS_OUTPUT_BYTES",
    "MAX_TTS_OUTPUT_SECONDS",
    "MAX_TTS_TEXT_CHARS",
    "TtsExecutionPolicy",
    "TtsFeatureService",
    "TtsProviderExecutor",
    "TtsProviderOutput",
    "TtsRequest",
    "require_tts_pricing",
]
