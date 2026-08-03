"""Google GenAI adapter for bounded provider-neutral video execution."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from google import genai
from google.genai import types

from derp.delivery.types import DeliveryMedia, TelegramMediaKind
from derp.execution import (
    ExecutionPlan,
    Failed,
    FailureReason,
    Outcome,
    Rejected,
    RejectionReason,
    Succeeded,
)
from derp.features.video import (
    VideoGenerateRequest,
    require_video_pricing,
)
from derp.media.types import normalize_mime_type

MAX_GOOGLE_VIDEO_START_DEADLINE_SECONDS = 120.0
MAX_GOOGLE_VIDEO_POLLING_DEADLINE_SECONDS = 600.0
MAX_GOOGLE_VIDEO_DOWNLOAD_DEADLINE_SECONDS = 120.0
MAX_GOOGLE_VIDEO_POLL_INTERVAL_SECONDS = 30.0
MAX_GOOGLE_VIDEO_POLL_ATTEMPTS = 120

type GoogleClientFactory = Callable[..., genai.Client]
type AsyncSleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class GoogleVideoExecutionPolicy:
    """Finite deadlines and polling limits for one Google video operation."""

    start_deadline_seconds: float = 30.0
    polling_deadline_seconds: float = 300.0
    download_deadline_seconds: float = 60.0
    poll_interval_seconds: float = 5.0
    max_poll_attempts: int = 60

    def __post_init__(self) -> None:
        limits = {
            "start_deadline_seconds": MAX_GOOGLE_VIDEO_START_DEADLINE_SECONDS,
            "polling_deadline_seconds": MAX_GOOGLE_VIDEO_POLLING_DEADLINE_SECONDS,
            "download_deadline_seconds": MAX_GOOGLE_VIDEO_DOWNLOAD_DEADLINE_SECONDS,
            "poll_interval_seconds": MAX_GOOGLE_VIDEO_POLL_INTERVAL_SECONDS,
        }
        for name, maximum in limits.items():
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 < value <= maximum
            ):
                raise ValueError(
                    f"{name} must be finite, positive, and at most {maximum}"
                )
        if isinstance(self.max_poll_attempts, bool) or not isinstance(
            self.max_poll_attempts, int
        ):
            raise TypeError("max_poll_attempts must be an integer")
        if not 1 <= self.max_poll_attempts <= MAX_GOOGLE_VIDEO_POLL_ATTEMPTS:
            raise ValueError(
                "max_poll_attempts must be between 1 and "
                f"{MAX_GOOGLE_VIDEO_POLL_ATTEMPTS}"
            )


class GoogleVideoExecutor:
    """Generate, finitely poll, and download exactly one Google MP4."""

    def __init__(
        self,
        api_key: str,
        *,
        policy: GoogleVideoExecutionPolicy | None = None,
        client_factory: GoogleClientFactory = genai.Client,
        sleep: AsyncSleep = asyncio.sleep,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("Google API key must not be blank")
        self._client = client_factory(api_key=api_key)
        self._policy = policy or GoogleVideoExecutionPolicy()
        self._sleep = sleep

    async def aclose(self) -> None:
        """Release transport resources owned by the Google SDK client."""
        await self._client.aio.aclose()

    async def generate(
        self,
        plan: ExecutionPlan,
        request: VideoGenerateRequest,
    ) -> Outcome[DeliveryMedia]:
        """Generate one MP4 with the exact catalog model and request controls."""
        pricing = require_video_pricing(plan)
        if request.duration_seconds not in pricing.supported_durations_seconds:
            return Rejected(RejectionReason.INVALID_INPUT)
        if request.resolution not in dict(pricing.output_per_second):
            return Rejected(RejectionReason.INVALID_INPUT)

        source_image = None
        if request.reference_image is not None:
            source_image = types.Image(
                image_bytes=request.reference_image.data,
                mime_type=request.reference_image.mime_type,
            )
        source = types.GenerateVideosSource(
            prompt=request.prompt,
            image=source_image,
        )
        config = types.GenerateVideosConfig(
            number_of_videos=1,
            duration_seconds=request.duration_seconds,
            aspect_ratio=request.aspect_ratio.value,
            resolution=request.resolution.value,
        )

        try:
            async with asyncio.timeout(self._policy.start_deadline_seconds):
                operation = await self._client.aio.models.generate_videos(
                    model=plan.model.provider_model_id,
                    source=source,
                    config=config,
                )
        except TimeoutError:
            return Failed(FailureReason.PROVIDER_ERROR)
        except Exception:
            return Failed(FailureReason.PROVIDER_ERROR)
        if not isinstance(operation, types.GenerateVideosOperation):
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)

        operation = await self._poll(operation)
        if operation is None:
            return Failed(FailureReason.PROVIDER_ERROR)
        if operation.error is not None:
            return Failed(FailureReason.PROVIDER_ERROR)
        response = operation.result or operation.response
        if not isinstance(response, types.GenerateVideosResponse):
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)

        generated = response.generated_videos or []
        if len(generated) != 1:
            if not generated and (
                bool(response.rai_media_filtered_count)
                or bool(response.rai_media_filtered_reasons)
            ):
                return Rejected(RejectionReason.POLICY)
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)
        video = generated[0].video
        if not isinstance(video, types.Video):
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)
        if video.mime_type is not None:
            try:
                if normalize_mime_type(video.mime_type) != "video/mp4":
                    return Rejected(RejectionReason.UNUSABLE_OUTPUT)
            except AttributeError, TypeError, ValueError:
                return Rejected(RejectionReason.UNUSABLE_OUTPUT)

        video_bytes = video.video_bytes
        if video_bytes is None:
            if not video.uri:
                return Rejected(RejectionReason.UNUSABLE_OUTPUT)
            try:
                async with asyncio.timeout(self._policy.download_deadline_seconds):
                    video_bytes = await self._client.aio.files.download(file=video)
            except TimeoutError:
                return Failed(FailureReason.PROVIDER_ERROR)
            except Exception:
                return Failed(FailureReason.PROVIDER_ERROR)
        if not isinstance(video_bytes, bytes) or not video_bytes:
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)

        try:
            return Succeeded(
                DeliveryMedia(
                    kind=TelegramMediaKind.VIDEO,
                    mime_type="video/mp4",
                    data=video_bytes,
                )
            )
        except Exception:
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)

    async def _poll(
        self,
        operation: types.GenerateVideosOperation,
    ) -> types.GenerateVideosOperation | None:
        if operation.done is True or operation.error is not None:
            return operation
        if not operation.name:
            return None
        try:
            async with asyncio.timeout(self._policy.polling_deadline_seconds):
                for _ in range(self._policy.max_poll_attempts):
                    await self._sleep(self._policy.poll_interval_seconds)
                    operation = await self._client.aio.operations.get(operation)
                    if not isinstance(operation, types.GenerateVideosOperation):
                        return None
                    if operation.done is True or operation.error is not None:
                        return operation
        except TimeoutError:
            return None
        except Exception:
            return None
        return None


__all__ = [
    "MAX_GOOGLE_VIDEO_DOWNLOAD_DEADLINE_SECONDS",
    "MAX_GOOGLE_VIDEO_POLL_ATTEMPTS",
    "MAX_GOOGLE_VIDEO_POLL_INTERVAL_SECONDS",
    "MAX_GOOGLE_VIDEO_POLLING_DEADLINE_SECONDS",
    "MAX_GOOGLE_VIDEO_START_DEADLINE_SECONDS",
    "GoogleVideoExecutionPolicy",
    "GoogleVideoExecutor",
]
