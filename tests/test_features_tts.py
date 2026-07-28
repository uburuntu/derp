"""Contracts for the provider-neutral text-to-speech feature boundary."""

from __future__ import annotations

import asyncio
import math
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from derp.catalog import AudioPricing, GoogleModelKey, InferenceProvider
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
from derp.features.tts import (
    MAX_TTS_OUTPUT_BYTES,
    MAX_TTS_OUTPUT_SECONDS,
    MAX_TTS_TEXT_CHARS,
    TtsExecutionPolicy,
    TtsFeatureService,
    TtsProviderOutput,
    TtsRequest,
    require_tts_pricing,
)

TTS_PLAN = plan_execution(
    Feature.TTS,
    GoogleModelKey.TTS,
    provider=InferenceProvider.GOOGLE,
)


def _provider_output(
    *,
    data: bytes = b"ogg",
    duration_seconds: float = 1.0,
) -> TtsProviderOutput:
    return TtsProviderOutput(
        media=DeliveryMedia(
            kind=TelegramMediaKind.VOICE,
            mime_type="audio/ogg",
            data=data,
        ),
        duration_seconds=duration_seconds,
    )


def test_tts_request_is_normalized_frozen_and_explicitly_bounded() -> None:
    request = TtsRequest(text="  Read this aloud.  ", max_output_seconds=12)

    assert request.text == "Read this aloud."
    assert request.max_output_seconds == 12
    with pytest.raises(FrozenInstanceError):
        request.text = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: TtsRequest(text="", max_output_seconds=1),
        lambda: TtsRequest(text=" " * 3, max_output_seconds=1),
        lambda: TtsRequest(text="x" * (MAX_TTS_TEXT_CHARS + 1), max_output_seconds=1),
        lambda: TtsRequest(text="hello", max_output_seconds=0),
        lambda: TtsRequest(
            text="hello",
            max_output_seconds=MAX_TTS_OUTPUT_SECONDS + 1,
        ),
    ],
)
def test_tts_request_rejects_invalid_values(factory) -> None:
    with pytest.raises(ValueError):
        factory()


@pytest.mark.parametrize("seconds", [True, 1.5, "1"])
def test_tts_request_requires_integer_seconds(seconds: object) -> None:
    with pytest.raises(TypeError, match="must be an integer"):
        TtsRequest(text="hello", max_output_seconds=seconds)  # type: ignore[arg-type]


def test_provider_output_is_delivery_ready_voice_media() -> None:
    output = _provider_output(duration_seconds=1)

    assert output.media.kind is TelegramMediaKind.VOICE
    assert output.media.mime_type == "audio/ogg"
    assert output.duration_seconds == 1.0
    with pytest.raises(ValueError, match="Telegram voice"):
        TtsProviderOutput(
            media=DeliveryMedia(
                kind=TelegramMediaKind.AUDIO,
                mime_type="audio/mpeg",
                data=b"audio",
            ),
            duration_seconds=1,
        )
    with pytest.raises(ValueError, match="finite and positive"):
        _provider_output(duration_seconds=math.inf)


def test_tts_policy_rejects_unbounded_configuration() -> None:
    policy = TtsExecutionPolicy()

    assert policy.max_output_seconds == MAX_TTS_OUTPUT_SECONDS
    assert policy.max_output_bytes == MAX_TTS_OUTPUT_BYTES
    with pytest.raises(TypeError, match="must be an integer"):
        TtsExecutionPolicy(max_output_bytes=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="must not exceed"):
        TtsExecutionPolicy(max_output_seconds=MAX_TTS_OUTPUT_SECONDS + 1)
    with pytest.raises(ValueError, match="must not exceed"):
        TtsExecutionPolicy(max_output_bytes=MAX_TTS_OUTPUT_BYTES + 1)
    with pytest.raises(ValueError, match="finite and positive"):
        TtsExecutionPolicy(provider_deadline_seconds=math.inf)


@pytest.mark.asyncio
async def test_service_passes_exact_plan_and_exposes_only_delivery_media() -> None:
    plan = TTS_PLAN
    request = TtsRequest(text="hello", max_output_seconds=3)
    provider_output = _provider_output()
    executor = SimpleNamespace(
        synthesize=AsyncMock(return_value=Succeeded(provider_output))
    )

    outcome = await TtsFeatureService(executor).synthesize(plan, request)

    assert outcome == Succeeded(provider_output.media)
    executor.synthesize.assert_awaited_once_with(plan, request)
    assert isinstance(require_tts_pricing(plan), AudioPricing)


@pytest.mark.asyncio
async def test_service_rejects_non_tts_plan_before_provider_execution() -> None:
    executor = SimpleNamespace(synthesize=AsyncMock())
    service = TtsFeatureService(executor)

    with pytest.raises(ValueError, match="cannot execute tts"):
        await service.synthesize(
            plan_execution(Feature.CHAT, GoogleModelKey.CHAT_STANDARD),
            TtsRequest(text="hello", max_output_seconds=3),
        )

    executor.synthesize.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_rejects_duration_above_product_policy_before_provider() -> None:
    executor = SimpleNamespace(synthesize=AsyncMock())
    service = TtsFeatureService(
        executor,
        policy=TtsExecutionPolicy(max_output_seconds=2),
    )

    outcome = await service.synthesize(
        TTS_PLAN,
        TtsRequest(text="hello", max_output_seconds=3),
    )

    assert outcome == Rejected(RejectionReason.INVALID_INPUT)
    executor.synthesize.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_deadline_cancels_execution_and_returns_typed_failure() -> None:
    class SlowExecutor:
        def __init__(self) -> None:
            self.cancelled = False

        async def synthesize(self, plan, request):
            try:
                await asyncio.sleep(60)
            finally:
                self.cancelled = True

    executor = SlowExecutor()
    service = TtsFeatureService(
        executor,
        policy=TtsExecutionPolicy(provider_deadline_seconds=0.001),
    )

    outcome = await service.synthesize(
        TTS_PLAN,
        TtsRequest(text="hello", max_output_seconds=3),
    )

    assert outcome == Failed(FailureReason.PROVIDER_ERROR)
    assert executor.cancelled


@pytest.mark.asyncio
async def test_service_preserves_typed_rejections_and_maps_provider_errors() -> None:
    plan = TTS_PLAN
    request = TtsRequest(text="hello", max_output_seconds=3)
    rejection = Rejected(RejectionReason.POLICY)
    executor = SimpleNamespace(synthesize=AsyncMock(return_value=rejection))
    service = TtsFeatureService(executor)

    assert await service.synthesize(plan, request) is rejection
    executor.synthesize.side_effect = RuntimeError("private provider detail")
    assert await service.synthesize(plan, request) == Failed(
        FailureReason.PROVIDER_ERROR
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_value", "policy"),
    [
        (_provider_output(duration_seconds=4), TtsExecutionPolicy()),
        (_provider_output(data=b"1234"), TtsExecutionPolicy(max_output_bytes=3)),
        (SimpleNamespace(media=b"not-media"), TtsExecutionPolicy()),
    ],
)
async def test_service_rejects_unusable_duration_bytes_or_payload(
    provider_value: object,
    policy: TtsExecutionPolicy,
) -> None:
    executor = SimpleNamespace(
        synthesize=AsyncMock(return_value=Succeeded(provider_value))
    )

    outcome = await TtsFeatureService(executor, policy=policy).synthesize(
        TTS_PLAN,
        TtsRequest(text="hello", max_output_seconds=3),
    )

    assert outcome == Rejected(RejectionReason.UNUSABLE_OUTPUT)
