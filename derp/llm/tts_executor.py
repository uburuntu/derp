"""Google GenAI adapter for provider-neutral text-to-speech execution."""

from __future__ import annotations

import wave
from collections.abc import Callable
from email.message import Message
from io import BytesIO
from typing import Protocol

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
from derp.features.tts import (
    MAX_TTS_OUTPUT_BYTES,
    TtsProviderOutput,
    TtsRequest,
    require_tts_pricing,
)
from derp.media.types import normalize_mime_type

GEMINI_TTS_SAMPLE_RATE = 24_000
_PCM_MIME_TYPES = frozenset({"audio/l16", "audio/pcm", "audio/x-pcm"})
_WAV_MIME_TYPES = frozenset({"audio/wav", "audio/x-wav"})
_POLICY_FINISH_REASONS = frozenset(
    {
        types.FinishReason.SAFETY,
        types.FinishReason.RECITATION,
        types.FinishReason.LANGUAGE,
        types.FinishReason.BLOCKLIST,
        types.FinishReason.PROHIBITED_CONTENT,
        types.FinishReason.SPII,
    }
)


class TtsAudioConverter(Protocol):
    """Convert bounded PCM or WAV source audio to OGG/Opus."""

    async def __call__(
        self,
        audio_bytes: bytes,
        *,
        input_format: str = "wav",
        sample_rate: int | None = None,
        channels: int = 1,
    ) -> bytes: ...


type GoogleClientFactory = Callable[..., genai.Client]


class GoogleTtsExecutor:
    """Call Google TTS and normalize one audio part without product effects."""

    def __init__(
        self,
        api_key: str,
        *,
        converter: TtsAudioConverter,
        client_factory: GoogleClientFactory = genai.Client,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("Google API key must not be blank")
        self._client = client_factory(api_key=api_key)
        self._converter = converter

    async def aclose(self) -> None:
        """Release transport resources owned by the Google SDK client."""
        await self._client.aio.aclose()

    async def synthesize(
        self,
        plan: ExecutionPlan,
        request: TtsRequest,
    ) -> Outcome[TtsProviderOutput]:
        """Generate one bounded voice using the exact catalog model."""
        pricing = require_tts_pricing(plan)
        max_output_tokens = pricing.audio_tokens_per_second * request.max_output_seconds
        if (
            plan.model.output_token_limit is not None
            and max_output_tokens > plan.model.output_token_limit
        ):
            return Rejected(RejectionReason.INVALID_INPUT)
        try:
            response = await self._client.aio.models.generate_content(
                model=plan.model.provider_model_id,
                contents=request.text,
                config=types.GenerateContentConfig(
                    candidate_count=1,
                    max_output_tokens=max_output_tokens,
                    response_modalities=["AUDIO"],
                ),
            )
        except Exception:
            return Failed(FailureReason.PROVIDER_ERROR)

        if not isinstance(response, types.GenerateContentResponse):
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)
        if self._is_policy_rejection(response):
            return Rejected(RejectionReason.POLICY)
        parts = self._response_parts(response)
        if parts is None:
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)

        inline_parts = [part.inline_data for part in parts if part.inline_data]
        if len(inline_parts) != 1:
            if not inline_parts and any(part.text is not None for part in parts):
                return Rejected(RejectionReason.POLICY)
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)
        blob = inline_parts[0]
        if not isinstance(blob.data, bytes) or not blob.data or not blob.mime_type:
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)
        if len(blob.data) > MAX_TTS_OUTPUT_BYTES:
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)

        try:
            input_format, sample_rate, channels, duration_seconds = (
                self._source_audio_metadata(blob.data, blob.mime_type)
            )
        except AttributeError, TypeError, ValueError, wave.Error, EOFError:
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)
        if duration_seconds > request.max_output_seconds:
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)

        try:
            converted = await self._converter(
                blob.data,
                input_format=input_format,
                sample_rate=sample_rate,
                channels=channels,
            )
        except Exception:
            return Failed(FailureReason.PROVIDER_ERROR)
        if not isinstance(converted, bytes) or not converted:
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)
        try:
            media = DeliveryMedia(
                kind=TelegramMediaKind.VOICE,
                mime_type="audio/ogg",
                data=converted,
            )
            return Succeeded(
                TtsProviderOutput(
                    media=media,
                    duration_seconds=duration_seconds,
                )
            )
        except Exception:
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)

    @staticmethod
    def _is_policy_rejection(response: types.GenerateContentResponse) -> bool:
        feedback = response.prompt_feedback
        if (
            feedback is not None
            and feedback.block_reason is not None
            and feedback.block_reason
            is not types.BlockedReason.BLOCKED_REASON_UNSPECIFIED
        ):
            return True
        candidates = response.candidates or []
        return any(
            candidate.finish_reason in _POLICY_FINISH_REASONS
            for candidate in candidates
        )

    @staticmethod
    def _response_parts(
        response: types.GenerateContentResponse,
    ) -> list[types.Part] | None:
        candidates = response.candidates or []
        if len(candidates) != 1 or candidates[0].content is None:
            return None
        return candidates[0].content.parts

    @staticmethod
    def _source_audio_metadata(
        audio_bytes: bytes,
        mime_type: str,
    ) -> tuple[str, int | None, int, float]:
        normalized = normalize_mime_type(mime_type)
        if normalized in _PCM_MIME_TYPES:
            sample_rate = GoogleTtsExecutor._mime_positive_int_parameter(
                mime_type,
                "rate",
                default=GEMINI_TTS_SAMPLE_RATE,
            )
            channels = GoogleTtsExecutor._mime_positive_int_parameter(
                mime_type,
                "channels",
                default=1,
            )
            frame_width = channels * 2
            if len(audio_bytes) % frame_width:
                raise ValueError("PCM data does not contain complete 16-bit frames")
            duration_seconds = len(audio_bytes) / (sample_rate * frame_width)
            if duration_seconds <= 0:
                raise ValueError("PCM duration must be positive")
            return "s16le", sample_rate, channels, duration_seconds
        if normalized in _WAV_MIME_TYPES:
            with wave.open(BytesIO(audio_bytes), "rb") as source:
                frame_rate = source.getframerate()
                channels = source.getnchannels()
                frames = source.getnframes()
                if frame_rate <= 0 or channels <= 0 or frames <= 0:
                    raise ValueError("WAV metadata must be positive")
                duration_seconds = frames / frame_rate
            return "wav", None, channels, duration_seconds
        raise ValueError("unsupported TTS source MIME type")

    @staticmethod
    def _mime_positive_int_parameter(
        mime_type: str,
        name: str,
        *,
        default: int,
    ) -> int:
        message = Message()
        message["content-type"] = mime_type
        raw_value = message.get_param(name, str(default), header="content-type")
        value = int(raw_value)
        if value <= 0:
            raise ValueError(f"{name} must be positive")
        return value


__all__ = [
    "GEMINI_TTS_SAMPLE_RATE",
    "GoogleTtsExecutor",
    "TtsAudioConverter",
]
