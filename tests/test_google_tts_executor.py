"""Google TTS responses are normalized at one content-free provider boundary."""

from __future__ import annotations

import ast
import inspect
import wave
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.genai import types

from derp.catalog import GoogleModelKey
from derp.execution import (
    Failed,
    FailureReason,
    Feature,
    Rejected,
    RejectionReason,
    Succeeded,
    plan_execution,
)
from derp.features import TtsProviderOutput, TtsRequest
from derp.llm.tts_executor import GEMINI_TTS_SAMPLE_RATE, GoogleTtsExecutor


def _response(
    *parts: types.Part,
    finish_reason: types.FinishReason = types.FinishReason.STOP,
) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(parts=list(parts), role="model"),
                finish_reason=finish_reason,
            )
        ]
    )


def _inline(data: bytes | None, mime_type: str | None) -> types.Part:
    return types.Part(inline_data=types.Blob(data=data, mime_type=mime_type))


def _wav_bytes(*, seconds: int = 1, sample_rate: int = 8_000) -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(bytes(sample_rate * seconds * 2))
    return output.getvalue()


def _executor(
    response: types.GenerateContentResponse,
    *,
    converted: bytes = b"ogg-opus",
):
    generate_content = AsyncMock(return_value=response)
    client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    )
    factory = MagicMock(return_value=client)
    converter = AsyncMock(return_value=converted)
    executor = GoogleTtsExecutor(
        "api-key",
        converter=converter,
        client_factory=factory,
    )
    return executor, factory, generate_content, converter


@pytest.mark.asyncio
async def test_pcm_generation_uses_exact_catalog_model_and_bounded_config() -> None:
    pcm = bytes(GEMINI_TTS_SAMPLE_RATE * 2)
    executor, factory, generate_content, converter = _executor(
        _response(_inline(pcm, "audio/L16;codec=pcm;rate=24000"))
    )
    plan = plan_execution(Feature.TTS, GoogleModelKey.TTS)

    outcome = await executor.synthesize(
        plan,
        TtsRequest(text="Read this", max_output_seconds=3),
    )

    assert isinstance(outcome, Succeeded)
    assert isinstance(outcome.value, TtsProviderOutput)
    assert outcome.value.duration_seconds == 1
    assert outcome.value.media.mime_type == "audio/ogg"
    assert outcome.value.media.data == b"ogg-opus"
    factory.assert_called_once_with(api_key="api-key")
    call = generate_content.await_args
    assert call.kwargs["model"] == plan.model.provider_model_id
    assert call.kwargs["contents"] == "Read this"
    config = call.kwargs["config"]
    assert config.candidate_count == 1
    assert config.response_modalities == ["AUDIO"]
    assert config.max_output_tokens == 75
    converter.assert_awaited_once_with(
        pcm,
        input_format="s16le",
        sample_rate=24_000,
        channels=1,
    )


@pytest.mark.asyncio
async def test_wav_generation_derives_duration_before_conversion() -> None:
    wav = _wav_bytes(seconds=2)
    executor, _, _, converter = _executor(_response(_inline(wav, "audio/wav")))

    outcome = await executor.synthesize(
        plan_execution(Feature.TTS, GoogleModelKey.TTS),
        TtsRequest(text="Read this", max_output_seconds=3),
    )

    assert isinstance(outcome, Succeeded)
    assert outcome.value.duration_seconds == 2
    converter.assert_awaited_once_with(
        wav,
        input_format="wav",
        sample_rate=None,
        channels=1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        types.GenerateContentResponse(
            prompt_feedback=types.GenerateContentResponsePromptFeedback(
                block_reason=types.BlockedReason.SAFETY
            )
        ),
        _response(types.Part(text="refusal")),
        _response(
            finish_reason=types.FinishReason.PROHIBITED_CONTENT,
        ),
    ],
)
async def test_provider_refusals_map_to_policy_without_reading_text(
    response: types.GenerateContentResponse,
) -> None:
    executor, _, _, converter = _executor(response)

    outcome = await executor.synthesize(
        plan_execution(Feature.TTS, GoogleModelKey.TTS),
        TtsRequest(text="private source text", max_output_seconds=3),
    )

    assert outcome == Rejected(RejectionReason.POLICY)
    converter.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        types.GenerateContentResponse(),
        types.GenerateContentResponse(candidates=[types.Candidate(content=None)]),
        types.GenerateContentResponse(
            candidates=[
                types.Candidate(content=types.Content(parts=[])),
                types.Candidate(content=types.Content(parts=[])),
            ]
        ),
        _response(_inline(None, "audio/L16;rate=24000")),
        _response(_inline(b"pcm", None)),
        _response(_inline(b"pcm", "image/png")),
        _response(_inline(b"pcm!", "audio/L16;rate=invalid")),
        _response(_inline(b"odd", "audio/L16;rate=24000")),
        _response(_inline(b"not-a-wave", "audio/wav")),
        _response(
            _inline(b"one!", "audio/L16;rate=24000"),
            _inline(b"two!", "audio/L16;rate=24000"),
        ),
    ],
)
async def test_missing_or_malformed_audio_is_unusable(
    response: types.GenerateContentResponse,
) -> None:
    executor, _, _, converter = _executor(response)

    outcome = await executor.synthesize(
        plan_execution(Feature.TTS, GoogleModelKey.TTS),
        TtsRequest(text="Read this", max_output_seconds=3),
    )

    assert outcome == Rejected(RejectionReason.UNUSABLE_OUTPUT)
    converter.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_duration_over_declared_limit_is_not_converted() -> None:
    pcm = bytes(GEMINI_TTS_SAMPLE_RATE * 2 * 4)
    executor, _, _, converter = _executor(
        _response(_inline(pcm, "audio/pcm;rate=24000"))
    )

    outcome = await executor.synthesize(
        plan_execution(Feature.TTS, GoogleModelKey.TTS),
        TtsRequest(text="Read this", max_output_seconds=3),
    )

    assert outcome == Rejected(RejectionReason.UNUSABLE_OUTPUT)
    converter.assert_not_awaited()


@pytest.mark.asyncio
async def test_conversion_failure_is_a_content_free_provider_failure() -> None:
    executor, _, _, converter = _executor(
        _response(_inline(b"pcm!", "audio/L16;rate=24000"))
    )
    converter.side_effect = RuntimeError("secret conversion detail")

    outcome = await executor.synthesize(
        plan_execution(Feature.TTS, GoogleModelKey.TTS),
        TtsRequest(text="private source text", max_output_seconds=3),
    )

    assert outcome == Failed(FailureReason.PROVIDER_ERROR)
    assert "secret" not in repr(outcome)


@pytest.mark.asyncio
@pytest.mark.parametrize("converted", [b"", bytearray(b"mutable")])
async def test_malformed_conversion_output_is_unusable(converted: object) -> None:
    executor, _, _, _ = _executor(
        _response(_inline(b"pcm!", "audio/L16;rate=24000")),
        converted=converted,  # type: ignore[arg-type]
    )

    outcome = await executor.synthesize(
        plan_execution(Feature.TTS, GoogleModelKey.TTS),
        TtsRequest(text="Read this", max_output_seconds=3),
    )

    assert outcome == Rejected(RejectionReason.UNUSABLE_OUTPUT)


@pytest.mark.asyncio
async def test_provider_exception_is_typed_without_exception_content() -> None:
    executor, _, generate_content, _ = _executor(types.GenerateContentResponse())
    generate_content.side_effect = RuntimeError("private provider response")

    outcome = await executor.synthesize(
        plan_execution(Feature.TTS, GoogleModelKey.TTS),
        TtsRequest(text="private prompt", max_output_seconds=3),
    )

    assert outcome == Failed(FailureReason.PROVIDER_ERROR)
    assert "private" not in repr(outcome)


@pytest.mark.asyncio
async def test_malformed_sdk_response_is_unusable() -> None:
    executor, _, generate_content, converter = _executor(
        types.GenerateContentResponse()
    )
    generate_content.return_value = SimpleNamespace(parts=[])

    outcome = await executor.synthesize(
        plan_execution(Feature.TTS, GoogleModelKey.TTS),
        TtsRequest(text="Read this", max_output_seconds=3),
    )

    assert outcome == Rejected(RejectionReason.UNUSABLE_OUTPUT)
    converter.assert_not_awaited()


def test_executor_rejects_blank_api_key_without_constructing_client() -> None:
    factory = MagicMock()

    with pytest.raises(ValueError, match="must not be blank"):
        GoogleTtsExecutor(" ", converter=AsyncMock(), client_factory=factory)

    factory.assert_not_called()


@pytest.mark.asyncio
async def test_executor_closes_its_async_google_client() -> None:
    client = SimpleNamespace(aio=SimpleNamespace(aclose=AsyncMock()))
    executor = GoogleTtsExecutor(
        "api-key",
        converter=AsyncMock(),
        client_factory=MagicMock(return_value=client),
    )

    await executor.aclose()

    client.aio.aclose.assert_awaited_once_with()


def test_new_tts_boundaries_do_not_import_product_side_effect_layers() -> None:
    from derp.features import tts as feature_module
    from derp.llm import tts_executor as executor_module

    forbidden = (
        "aiogram",
        "logfire",
        "derp.common.sender",
        "derp.config",
        "derp.credits",
        "derp.db",
        "derp.models",
    )
    for module in (feature_module, executor_module):
        tree = ast.parse(inspect.getsource(module))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert not any(
            name == prefix or name.startswith(f"{prefix}.")
            for name in imported
            for prefix in forbidden
        )
