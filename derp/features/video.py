"""Provider-neutral video requests and bounded execution service."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from derp.catalog import GoogleModelKey, VideoPricing, VideoResolution
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
from derp.media.types import normalize_mime_type

if TYPE_CHECKING:
    from derp.delivery.types import DeliveryMedia

MAX_VIDEO_PROMPT_CHARS = 4_096
MAX_VIDEO_REFERENCE_BYTES = 20 * 1024 * 1024
MAX_VIDEO_OUTPUT_BYTES = 50 * 1024 * 1024
MAX_VIDEO_PROVIDER_DEADLINE_SECONDS = 600.0
SUPPORTED_VIDEO_DURATIONS = frozenset({4, 6, 8})
SUPPORTED_VIDEO_REFERENCE_MIME_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/webp"}
)
_VIDEO_MODEL_KEYS = frozenset(
    {GoogleModelKey.VIDEO_FAST, GoogleModelKey.VIDEO_STANDARD}
)


class VideoAspectRatio(StrEnum):
    """Aspect ratios accepted by the installed Veo SDK surface."""

    LANDSCAPE = "16:9"
    PORTRAIT = "9:16"


@dataclass(frozen=True, slots=True)
class VideoReferenceImage:
    """Hydrated immutable image ready for a provider adapter."""

    data: bytes
    mime_type: str

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError("video reference data must be immutable bytes")
        if not self.data:
            raise ValueError("video reference data must not be empty")
        if len(self.data) > MAX_VIDEO_REFERENCE_BYTES:
            raise ValueError(
                f"video reference data must not exceed {MAX_VIDEO_REFERENCE_BYTES} bytes"
            )
        mime_type = normalize_mime_type(self.mime_type)
        if mime_type not in SUPPORTED_VIDEO_REFERENCE_MIME_TYPES:
            raise ValueError("unsupported video reference MIME type")
        object.__setattr__(self, "mime_type", mime_type)


@dataclass(frozen=True, slots=True)
class VideoGenerateRequest:
    """Validated provider-independent Veo generation request."""

    prompt: str
    duration_seconds: int = 6
    aspect_ratio: VideoAspectRatio = VideoAspectRatio.LANDSCAPE
    resolution: VideoResolution = VideoResolution.HD_720P
    reference_image: VideoReferenceImage | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.prompt, str):
            raise TypeError("video prompt must be a string")
        prompt = self.prompt.strip()
        if not prompt:
            raise ValueError("video prompt must not be blank")
        if len(prompt) > MAX_VIDEO_PROMPT_CHARS:
            raise ValueError(
                f"video prompt must be at most {MAX_VIDEO_PROMPT_CHARS} characters"
            )
        if isinstance(self.duration_seconds, bool) or not isinstance(
            self.duration_seconds, int
        ):
            raise TypeError("duration_seconds must be an integer")
        if self.duration_seconds not in SUPPORTED_VIDEO_DURATIONS:
            raise ValueError(
                f"duration_seconds must be one of {sorted(SUPPORTED_VIDEO_DURATIONS)}"
            )
        if not isinstance(self.aspect_ratio, VideoAspectRatio):
            raise TypeError("aspect_ratio must be a VideoAspectRatio")
        if not isinstance(self.resolution, VideoResolution):
            raise TypeError("resolution must be a VideoResolution")
        if self.reference_image is not None and not isinstance(
            self.reference_image, VideoReferenceImage
        ):
            raise TypeError("reference_image must be a VideoReferenceImage")
        object.__setattr__(self, "prompt", prompt)


@dataclass(frozen=True, slots=True)
class VideoExecutionPolicy:
    """Resource and overall deadline limits around one video provider."""

    max_reference_bytes: int = MAX_VIDEO_REFERENCE_BYTES
    max_output_bytes: int = MAX_VIDEO_OUTPUT_BYTES
    provider_deadline_seconds: float = 420.0

    def __post_init__(self) -> None:
        for name in ("max_reference_bytes", "max_output_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_reference_bytes > MAX_VIDEO_REFERENCE_BYTES:
            raise ValueError(
                f"max_reference_bytes must not exceed {MAX_VIDEO_REFERENCE_BYTES}"
            )
        if self.max_output_bytes > MAX_VIDEO_OUTPUT_BYTES:
            raise ValueError(
                f"max_output_bytes must not exceed {MAX_VIDEO_OUTPUT_BYTES}"
            )
        if (
            isinstance(self.provider_deadline_seconds, bool)
            or not isinstance(self.provider_deadline_seconds, (int, float))
            or not math.isfinite(self.provider_deadline_seconds)
            or not 0
            < self.provider_deadline_seconds
            <= MAX_VIDEO_PROVIDER_DEADLINE_SECONDS
        ):
            raise ValueError(
                "provider_deadline_seconds must be finite, positive, and at most "
                f"{MAX_VIDEO_PROVIDER_DEADLINE_SECONDS}"
            )


class VideoProviderExecutor(Protocol):
    """Provider adapter with no Telegram, database, or billing effects."""

    async def generate(
        self,
        plan: ExecutionPlan,
        request: VideoGenerateRequest,
    ) -> Outcome[DeliveryMedia]:
        """Generate one MP4 using the exact execution plan."""
        ...


class VideoFeatureService:
    """Validate video policy and expose delivery-ready MP4 media."""

    def __init__(
        self,
        executor: VideoProviderExecutor,
        *,
        policy: VideoExecutionPolicy | None = None,
    ) -> None:
        self._executor = executor
        self._policy = policy or VideoExecutionPolicy()

    async def generate(
        self,
        plan: ExecutionPlan,
        request: VideoGenerateRequest,
    ) -> Outcome[DeliveryMedia]:
        """Generate one video within catalog, resource, and deadline limits."""
        pricing = require_video_pricing(plan)
        if request.duration_seconds not in pricing.supported_durations_seconds:
            return Rejected(RejectionReason.INVALID_INPUT)
        if request.resolution not in dict(pricing.output_per_second):
            return Rejected(RejectionReason.INVALID_INPUT)
        if (
            request.reference_image is not None
            and len(request.reference_image.data) > self._policy.max_reference_bytes
        ):
            return Rejected(RejectionReason.INVALID_INPUT)
        return await self._run_provider(self._executor.generate(plan, request))

    async def _run_provider(
        self,
        execution: Awaitable[Outcome[DeliveryMedia]],
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

        from derp.delivery.types import DeliveryMedia, TelegramMediaKind

        media = outcome.value
        if not isinstance(media, DeliveryMedia):
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)
        if media.kind is not TelegramMediaKind.VIDEO or media.mime_type != "video/mp4":
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)
        if len(media.data) > self._policy.max_output_bytes:
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)
        return outcome


def require_video_pricing(plan: ExecutionPlan) -> VideoPricing:
    """Return pricing only for canonical fast or standard video plans."""
    if plan.feature is not Feature.VIDEO_GENERATE:
        raise ValueError(
            f"{plan.feature.value} cannot execute {Feature.VIDEO_GENERATE.value}"
        )
    if plan.model.key not in _VIDEO_MODEL_KEYS:
        raise ValueError(f"{plan.model.key.value} is not a catalog video model")
    if not isinstance(plan.model.pricing, VideoPricing):
        raise ValueError(f"{plan.model.key.value} does not use video pricing")
    return plan.model.pricing


__all__ = [
    "MAX_VIDEO_OUTPUT_BYTES",
    "MAX_VIDEO_PROMPT_CHARS",
    "MAX_VIDEO_PROVIDER_DEADLINE_SECONDS",
    "MAX_VIDEO_REFERENCE_BYTES",
    "SUPPORTED_VIDEO_DURATIONS",
    "SUPPORTED_VIDEO_REFERENCE_MIME_TYPES",
    "VideoAspectRatio",
    "VideoExecutionPolicy",
    "VideoFeatureService",
    "VideoGenerateRequest",
    "VideoProviderExecutor",
    "VideoReferenceImage",
    "require_video_pricing",
]
