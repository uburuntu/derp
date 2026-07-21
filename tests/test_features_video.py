"""Contracts for the provider-neutral video feature boundary."""

from __future__ import annotations

import asyncio
import math
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from derp.catalog import (
    GoogleModelKey,
    VideoPricing,
    VideoResolution,
    get_google_model,
)
from derp.delivery.types import DeliveryMedia, TelegramMediaKind
from derp.execution import (
    Failed,
    FailureReason,
    Feature,
    Rejected,
    RejectionReason,
    Succeeded,
    plan_execution,
)
from derp.features.video import (
    MAX_VIDEO_OUTPUT_BYTES,
    MAX_VIDEO_PROMPT_CHARS,
    MAX_VIDEO_PROVIDER_DEADLINE_SECONDS,
    MAX_VIDEO_REFERENCE_BYTES,
    SUPPORTED_VIDEO_DURATIONS,
    VideoAspectRatio,
    VideoExecutionPolicy,
    VideoFeatureService,
    VideoGenerateRequest,
    VideoReferenceImage,
    require_video_pricing,
)


def _video(data: bytes = b"mp4") -> DeliveryMedia:
    return DeliveryMedia(
        kind=TelegramMediaKind.VIDEO,
        mime_type="video/mp4",
        data=data,
    )


def test_video_request_is_normalized_frozen_and_provider_neutral() -> None:
    reference = VideoReferenceImage(
        data=b"image",
        mime_type="IMAGE/PNG; charset=binary",
    )
    request = VideoGenerateRequest(
        prompt="  A lighthouse in a storm  ",
        duration_seconds=8,
        aspect_ratio=VideoAspectRatio.PORTRAIT,
        resolution=VideoResolution.HD_1080P,
        reference_image=reference,
    )

    assert request.prompt == "A lighthouse in a storm"
    assert request.duration_seconds == 8
    assert request.aspect_ratio is VideoAspectRatio.PORTRAIT
    assert request.resolution is VideoResolution.HD_1080P
    assert reference.mime_type == "image/png"
    with pytest.raises(FrozenInstanceError):
        request.prompt = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: VideoGenerateRequest(prompt=""),
        lambda: VideoGenerateRequest(prompt=" " * 3),
        lambda: VideoGenerateRequest(prompt="x" * (MAX_VIDEO_PROMPT_CHARS + 1)),
        lambda: VideoGenerateRequest(prompt="video", duration_seconds=5),
    ],
)
def test_video_request_rejects_invalid_prompt_or_duration(factory) -> None:
    with pytest.raises(ValueError):
        factory()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("prompt", 1, "prompt must be a string"),
        ("duration_seconds", True, "duration_seconds must be an integer"),
        ("aspect_ratio", "16:9", "VideoAspectRatio"),
        ("resolution", "720p", "VideoResolution"),
        ("reference_image", b"image", "VideoReferenceImage"),
    ],
)
def test_video_request_requires_typed_values(
    name: str,
    value: object,
    message: str,
) -> None:
    kwargs = {"prompt": "video", name: value}
    with pytest.raises(TypeError, match=message):
        VideoGenerateRequest(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: VideoReferenceImage(data=b"", mime_type="image/png"),
        lambda: VideoReferenceImage(
            data=bytes(MAX_VIDEO_REFERENCE_BYTES + 1),
            mime_type="image/png",
        ),
        lambda: VideoReferenceImage(data=b"image", mime_type="image/gif"),
    ],
)
def test_reference_image_rejects_empty_oversized_or_unsupported_data(factory) -> None:
    with pytest.raises(ValueError):
        factory()


def test_reference_image_requires_immutable_bytes() -> None:
    with pytest.raises(TypeError, match="immutable bytes"):
        VideoReferenceImage(  # type: ignore[arg-type]
            data=bytearray(b"image"),
            mime_type="image/png",
        )


def test_request_duration_contract_matches_both_catalog_video_models() -> None:
    for key in (GoogleModelKey.VIDEO_FAST, GoogleModelKey.VIDEO_STANDARD):
        pricing = get_google_model(key).pricing
        assert isinstance(pricing, VideoPricing)
        assert pricing.supported_durations_seconds == SUPPORTED_VIDEO_DURATIONS


def test_video_policy_has_finite_telegram_compatible_bounds() -> None:
    policy = VideoExecutionPolicy()

    assert policy.max_reference_bytes == MAX_VIDEO_REFERENCE_BYTES
    assert policy.max_output_bytes == MAX_VIDEO_OUTPUT_BYTES
    with pytest.raises(TypeError, match="must be an integer"):
        VideoExecutionPolicy(max_output_bytes=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must not exceed"):
        VideoExecutionPolicy(max_reference_bytes=MAX_VIDEO_REFERENCE_BYTES + 1)
    with pytest.raises(ValueError, match="must not exceed"):
        VideoExecutionPolicy(max_output_bytes=MAX_VIDEO_OUTPUT_BYTES + 1)
    with pytest.raises(ValueError, match="finite, positive"):
        VideoExecutionPolicy(provider_deadline_seconds=math.inf)
    with pytest.raises(ValueError, match="at most"):
        VideoExecutionPolicy(
            provider_deadline_seconds=MAX_VIDEO_PROVIDER_DEADLINE_SECONDS + 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_key",
    [GoogleModelKey.VIDEO_FAST, GoogleModelKey.VIDEO_STANDARD],
)
async def test_service_passes_exact_video_plan_and_returns_delivery_media(
    model_key: GoogleModelKey,
) -> None:
    plan = plan_execution(Feature.VIDEO_GENERATE, model_key)
    request = VideoGenerateRequest(prompt="A lighthouse")
    success = Succeeded(_video())
    executor = SimpleNamespace(generate=AsyncMock(return_value=success))

    outcome = await VideoFeatureService(executor).generate(plan, request)

    assert outcome is success
    executor.generate.assert_awaited_once_with(plan, request)
    assert isinstance(require_video_pricing(plan), VideoPricing)


@pytest.mark.asyncio
async def test_service_rejects_non_video_plan_before_provider_execution() -> None:
    executor = SimpleNamespace(generate=AsyncMock())

    with pytest.raises(ValueError, match="cannot execute video_generate"):
        await VideoFeatureService(executor).generate(
            plan_execution(Feature.CHAT, GoogleModelKey.CHAT_STANDARD),
            VideoGenerateRequest(prompt="A lighthouse"),
        )

    executor.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_rejects_reference_above_deployment_policy() -> None:
    executor = SimpleNamespace(generate=AsyncMock())
    request = VideoGenerateRequest(
        prompt="Animate this",
        reference_image=VideoReferenceImage(data=b"image", mime_type="image/png"),
    )

    outcome = await VideoFeatureService(
        executor,
        policy=VideoExecutionPolicy(max_reference_bytes=4),
    ).generate(
        plan_execution(Feature.VIDEO_GENERATE, GoogleModelKey.VIDEO_FAST),
        request,
    )

    assert outcome == Rejected(RejectionReason.INVALID_INPUT)
    executor.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_deadline_cancels_execution_and_returns_typed_failure() -> None:
    class SlowExecutor:
        def __init__(self) -> None:
            self.cancelled = False

        async def generate(self, plan, request):
            try:
                await asyncio.sleep(60)
            finally:
                self.cancelled = True

    executor = SlowExecutor()
    service = VideoFeatureService(
        executor,
        policy=VideoExecutionPolicy(provider_deadline_seconds=0.001),
    )

    outcome = await service.generate(
        plan_execution(Feature.VIDEO_GENERATE, GoogleModelKey.VIDEO_FAST),
        VideoGenerateRequest(prompt="A lighthouse"),
    )

    assert outcome == Failed(FailureReason.PROVIDER_ERROR)
    assert executor.cancelled


@pytest.mark.asyncio
async def test_service_preserves_typed_rejections_and_maps_provider_errors() -> None:
    plan = plan_execution(Feature.VIDEO_GENERATE, GoogleModelKey.VIDEO_FAST)
    request = VideoGenerateRequest(prompt="A lighthouse")
    rejection = Rejected(RejectionReason.POLICY)
    executor = SimpleNamespace(generate=AsyncMock(return_value=rejection))
    service = VideoFeatureService(executor)

    assert await service.generate(plan, request) is rejection
    executor.generate.side_effect = RuntimeError("private provider detail")
    assert await service.generate(plan, request) == Failed(FailureReason.PROVIDER_ERROR)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_value", "policy"),
    [
        (
            DeliveryMedia(
                kind=TelegramMediaKind.DOCUMENT,
                mime_type="video/mp4",
                data=b"video",
            ),
            VideoExecutionPolicy(),
        ),
        (_video(b"1234"), VideoExecutionPolicy(max_output_bytes=3)),
        (SimpleNamespace(data=b"video"), VideoExecutionPolicy()),
    ],
)
async def test_service_rejects_wrong_presentation_oversize_or_payload(
    provider_value: object,
    policy: VideoExecutionPolicy,
) -> None:
    executor = SimpleNamespace(
        generate=AsyncMock(return_value=Succeeded(provider_value))
    )

    outcome = await VideoFeatureService(executor, policy=policy).generate(
        plan_execution(Feature.VIDEO_GENERATE, GoogleModelKey.VIDEO_FAST),
        VideoGenerateRequest(prompt="A lighthouse"),
    )

    assert outcome == Rejected(RejectionReason.UNUSABLE_OUTPUT)
