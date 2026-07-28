"""Shared, retry-free async HTTP transport for dedicated OpenRouter APIs."""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from typing import Any, Literal, overload
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from derp.openrouter.errors import (
    OpenRouterHTTPError,
    OpenRouterResponseError,
    OpenRouterTransportError,
    ResponseFailureKind,
    TransportFailureKind,
)
from derp.openrouter.types import (
    CreditBalance,
    CurrentKeyInfo,
    GeneratedImage,
    GenerationMetadata,
    ImageGenerationRequest,
    ImageGenerationResult,
    ModelListQuery,
    ModelsPage,
    OpenRouterTimeouts,
    SpeechRequest,
    SpeechResult,
    TranscriptionRequest,
    TranscriptionResult,
    VideoContent,
    VideoGenerationRequest,
    VideoJob,
    _CreditsEnvelope,
    _CurrentKeyEnvelope,
    _GenerationEnvelope,
    _ImageEnvelope,
    _TranscriptionEnvelope,
    _VideoJobEnvelope,
)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
GENERATION_ID_HEADER = "X-Generation-Id"
_MAX_METADATA_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_IMAGE_RESPONSE_BYTES = 24 * 1024 * 1024
_MAX_AUDIO_RESPONSE_BYTES = 20 * 1024 * 1024
_MAX_TRANSCRIPTION_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_VIDEO_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_VIDEO_CONTENT_BYTES = 50 * 1024 * 1024


class OpenRouterClient:
    """Reuse one authenticated client across typed OpenRouter REST calls."""

    def __init__(
        self,
        *,
        api_key: str,
        app_url: str,
        app_title: str,
        http_client: httpx.AsyncClient | None = None,
        timeouts: OpenRouterTimeouts | None = None,
    ) -> None:
        _require_header_value("api_key", api_key, max_length=2048)
        _require_header_value("app_title", app_title, max_length=256)
        _require_header_value("app_url", app_url, max_length=2048)
        parsed_url = urlsplit(app_url)
        if parsed_url.scheme != "https" or not parsed_url.netloc:
            raise ValueError("app_url must be an absolute HTTPS URL")

        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": app_url,
            "X-OpenRouter-Title": app_title,
        }
        self._timeouts = timeouts or OpenRouterTimeouts()
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(retries=0),
            follow_redirects=False,
        )

    async def __aenter__(self) -> OpenRouterClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close only the internally owned shared HTTP client."""
        if self._owns_client:
            await self._client.aclose()

    async def list_models(
        self,
        query: ModelListQuery | None = None,
    ) -> ModelsPage:
        """Return one typed page from the current OpenRouter model catalog."""
        response = await self._request(
            "GET",
            "/models",
            expected_status=200,
            params=(query or ModelListQuery()).to_params(),
            timeout=self._timeouts.metadata,
            accept="application/json",
            max_response_bytes=_MAX_METADATA_RESPONSE_BYTES,
        )
        return _parse_json_model(response, ModelsPage, endpoint="/models")

    async def get_credits(self) -> CreditBalance:
        """Return total purchased and consumed OpenRouter credits."""
        response = await self._request(
            "GET",
            "/credits",
            expected_status=200,
            timeout=self._timeouts.metadata,
            accept="application/json",
            max_response_bytes=_MAX_METADATA_RESPONSE_BYTES,
        )
        return _parse_json_model(
            response,
            _CreditsEnvelope,
            endpoint="/credits",
        ).data

    async def get_current_key(self) -> CurrentKeyInfo:
        """Return spend and limit metadata for the authenticated inference key."""
        response = await self._request(
            "GET",
            "/key",
            expected_status=200,
            timeout=self._timeouts.metadata,
            accept="application/json",
            max_response_bytes=_MAX_METADATA_RESPONSE_BYTES,
        )
        return _parse_json_model(
            response,
            _CurrentKeyEnvelope,
            endpoint="/key",
        ).data

    async def get_generation(self, generation_id: str) -> GenerationMetadata:
        """Return final request, usage, routing, and actual-cost metadata."""
        _require_wire_identifier("generation_id", generation_id)
        response = await self._request(
            "GET",
            "/generation",
            expected_status=200,
            params={"id": generation_id},
            timeout=self._timeouts.metadata,
            accept="application/json",
            max_response_bytes=_MAX_METADATA_RESPONSE_BYTES,
        )
        return _parse_json_model(
            response,
            _GenerationEnvelope,
            endpoint="/generation",
        ).data

    async def generate_image(
        self,
        request: ImageGenerationRequest,
    ) -> ImageGenerationResult:
        """Run one buffered image request without automatic retry."""
        response = await self._request(
            "POST",
            "/images",
            expected_status=200,
            json_body=request.to_payload(),
            timeout=self._timeouts.generation,
            accept="application/json",
            max_response_bytes=_MAX_IMAGE_RESPONSE_BYTES,
        )
        envelope = _parse_json_model(response, _ImageEnvelope, endpoint="/images")
        if not envelope.data:
            raise OpenRouterResponseError(
                kind=ResponseFailureKind.EMPTY_BODY,
                endpoint="/images",
            )

        images: list[GeneratedImage] = []
        for item in envelope.data:
            try:
                data = base64.b64decode(item.b64_json, validate=True)
            except ValueError, TypeError:
                raise OpenRouterResponseError(
                    kind=ResponseFailureKind.INVALID_MEDIA,
                    endpoint="/images",
                ) from None
            if not data or (
                item.media_type is not None
                and not item.media_type.casefold().startswith("image/")
            ):
                raise OpenRouterResponseError(
                    kind=ResponseFailureKind.INVALID_MEDIA,
                    endpoint="/images",
                )
            images.append(GeneratedImage(data=data, media_type=item.media_type))

        return ImageGenerationResult(
            created=envelope.created,
            images=tuple(images),
            usage=envelope.usage,
            generation_id=_generation_id(response, required=False, endpoint="/images"),
        )

    async def synthesize_speech(self, request: SpeechRequest) -> SpeechResult:
        """Run one TTS request and require its accounting generation header."""
        response = await self._request(
            "POST",
            "/audio/speech",
            expected_status=200,
            json_body=request.to_payload(),
            timeout=self._timeouts.generation,
            accept="audio/*",
            max_response_bytes=_MAX_AUDIO_RESPONSE_BYTES,
        )
        media_type = _media_type(response)
        if not media_type.startswith("audio/") or not response.content:
            raise OpenRouterResponseError(
                kind=(
                    ResponseFailureKind.EMPTY_BODY
                    if not response.content
                    else ResponseFailureKind.UNEXPECTED_CONTENT_TYPE
                ),
                endpoint="/audio/speech",
            )
        generation_id = _generation_id(
            response,
            required=True,
            endpoint="/audio/speech",
        )
        return SpeechResult(
            data=response.content,
            media_type=media_type,
            generation_id=generation_id,
        )

    async def transcribe(
        self,
        request: TranscriptionRequest,
    ) -> TranscriptionResult:
        """Run one base64 JSON transcription request without automatic retry."""
        response = await self._request(
            "POST",
            "/audio/transcriptions",
            expected_status=200,
            json_body=request.to_payload(),
            timeout=self._timeouts.generation,
            accept="application/json",
            max_response_bytes=_MAX_TRANSCRIPTION_RESPONSE_BYTES,
        )
        envelope = _parse_json_model(
            response,
            _TranscriptionEnvelope,
            endpoint="/audio/transcriptions",
        )
        generation_id = _generation_id(
            response,
            required=True,
            endpoint="/audio/transcriptions",
        )
        return TranscriptionResult(
            text=envelope.text,
            usage=envelope.usage,
            generation_id=generation_id,
            task=envelope.task,
            language=envelope.language,
            duration=envelope.duration,
            segments=envelope.segments,
            words=envelope.words,
        )

    async def submit_video(self, request: VideoGenerationRequest) -> VideoJob:
        """Submit one asynchronous video job without automatic retry."""
        response = await self._request(
            "POST",
            "/videos",
            expected_status=202,
            json_body=request.to_payload(),
            timeout=self._timeouts.video_job,
            accept="application/json",
            max_response_bytes=_MAX_VIDEO_RESPONSE_BYTES,
        )
        envelope = _parse_json_model(
            response,
            _VideoJobEnvelope,
            endpoint="/videos",
        )
        return _video_job(envelope, endpoint="/videos")

    async def get_video_job(self, job_id: str) -> VideoJob:
        """Poll one video job once; callers own polling cadence and limits."""
        path = _video_path(job_id)
        response = await self._request(
            "GET",
            path,
            expected_status=200,
            timeout=self._timeouts.video_job,
            accept="application/json",
            max_response_bytes=_MAX_VIDEO_RESPONSE_BYTES,
        )
        envelope = _parse_json_model(response, _VideoJobEnvelope, endpoint=path)
        if envelope.id != job_id:
            raise OpenRouterResponseError(
                kind=ResponseFailureKind.INVALID_SCHEMA,
                endpoint=path,
            )
        return _video_job(envelope, endpoint=path)

    async def get_video_content(self, job_id: str, *, index: int = 0) -> VideoContent:
        """Download one output through OpenRouter's authenticated content proxy."""
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError("video content index must be a non-negative integer")
        path = f"{_video_path(job_id)}/content"
        response = await self._request(
            "GET",
            path,
            expected_status=200,
            params={"index": index},
            timeout=self._timeouts.media,
            accept="video/*",
            max_response_bytes=_MAX_VIDEO_CONTENT_BYTES,
        )
        media_type = _media_type(response)
        if not media_type.startswith("video/") or not response.content:
            raise OpenRouterResponseError(
                kind=(
                    ResponseFailureKind.EMPTY_BODY
                    if not response.content
                    else ResponseFailureKind.UNEXPECTED_CONTENT_TYPE
                ),
                endpoint=path,
            )
        return VideoContent(data=response.content, media_type=media_type)

    async def _request(
        self,
        method: Literal["GET", "POST"],
        endpoint: str,
        *,
        expected_status: int,
        timeout: httpx.Timeout,
        accept: str,
        max_response_bytes: int,
        params: dict[str, str | int] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        headers = {**self._headers, "Accept": accept}
        try:
            async with self._client.stream(
                method,
                f"{OPENROUTER_BASE_URL}{endpoint}",
                params=params,
                json=json_body,
                headers=headers,
                follow_redirects=False,
                timeout=timeout,
            ) as streamed:
                content = await _read_bounded(
                    streamed,
                    endpoint=endpoint,
                    max_bytes=max_response_bytes,
                )
                response = httpx.Response(
                    streamed.status_code,
                    headers=streamed.headers,
                    content=content,
                    request=streamed.request,
                    extensions=streamed.extensions,
                )
        except httpx.TimeoutException:
            raise OpenRouterTransportError(
                kind=TransportFailureKind.TIMEOUT,
                method=method,
                endpoint=endpoint,
            ) from None
        except httpx.RequestError:
            raise OpenRouterTransportError(
                kind=TransportFailureKind.NETWORK,
                method=method,
                endpoint=endpoint,
            ) from None

        if response.status_code != expected_status:
            raise _http_error(response, method=method, endpoint=endpoint)
        return response


async def _read_bounded(
    response: httpx.Response,
    *,
    endpoint: str,
    max_bytes: int,
) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length and content_length.isascii() and content_length.isdecimal():
        if int(content_length) > max_bytes:
            raise OpenRouterResponseError(
                kind=ResponseFailureKind.RESPONSE_TOO_LARGE,
                endpoint=endpoint,
            )
    chunks: list[bytes] = []
    received = 0
    async for chunk in response.aiter_bytes():
        received += len(chunk)
        if received > max_bytes:
            raise OpenRouterResponseError(
                kind=ResponseFailureKind.RESPONSE_TOO_LARGE,
                endpoint=endpoint,
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_json_model[ResponseModelT: BaseModel](
    response: httpx.Response,
    model: type[ResponseModelT],
    *,
    endpoint: str,
) -> ResponseModelT:
    media_type = _media_type(response)
    if media_type != "application/json" and not media_type.endswith("+json"):
        raise OpenRouterResponseError(
            kind=ResponseFailureKind.UNEXPECTED_CONTENT_TYPE,
            endpoint=endpoint,
        )
    if not response.content:
        raise OpenRouterResponseError(
            kind=ResponseFailureKind.EMPTY_BODY,
            endpoint=endpoint,
        )
    try:
        payload = json.loads(response.content, parse_float=Decimal)
    except json.JSONDecodeError, UnicodeDecodeError:
        raise OpenRouterResponseError(
            kind=ResponseFailureKind.INVALID_JSON,
            endpoint=endpoint,
        ) from None
    try:
        return model.model_validate(payload)
    except ValidationError:
        raise OpenRouterResponseError(
            kind=ResponseFailureKind.INVALID_SCHEMA,
            endpoint=endpoint,
        ) from None


def _video_job(envelope: _VideoJobEnvelope, *, endpoint: str) -> VideoJob:
    try:
        _require_wire_identifier("video job ID", envelope.id)
        if envelope.generation_id is not None:
            _require_wire_identifier("generation_id", envelope.generation_id)
    except ValueError:
        raise OpenRouterResponseError(
            kind=ResponseFailureKind.INVALID_SCHEMA,
            endpoint=endpoint,
        ) from None
    if not envelope.polling_url.strip():
        raise OpenRouterResponseError(
            kind=ResponseFailureKind.INVALID_SCHEMA,
            endpoint=endpoint,
        )
    return VideoJob(
        id=envelope.id,
        polling_url=envelope.polling_url,
        status=envelope.status,
        generation_id=envelope.generation_id,
        unsigned_urls=envelope.unsigned_urls,
        usage=envelope.usage,
        error_present=envelope.error is not None,
    )


@overload
def _generation_id(
    response: httpx.Response,
    *,
    required: Literal[True],
    endpoint: str,
) -> str: ...


@overload
def _generation_id(
    response: httpx.Response,
    *,
    required: Literal[False],
    endpoint: str,
) -> str | None: ...


def _generation_id(
    response: httpx.Response,
    *,
    required: bool,
    endpoint: str,
) -> str | None:
    value = response.headers.get(GENERATION_ID_HEADER)
    if value is None and not required:
        return None
    try:
        _require_wire_identifier("generation_id", value)
    except ValueError:
        raise OpenRouterResponseError(
            kind=ResponseFailureKind.MISSING_GENERATION_ID,
            endpoint=endpoint,
        ) from None
    return value


def _http_error(
    response: httpx.Response,
    *,
    method: str,
    endpoint: str,
) -> OpenRouterHTTPError:
    error_code: int | str | None = None
    if response.content and (
        _media_type(response) == "application/json"
        or _media_type(response).endswith("+json")
    ):
        try:
            payload = json.loads(response.content)
            error = payload.get("error") if isinstance(payload, dict) else None
            candidate = error.get("code") if isinstance(error, dict) else None
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                error_code = candidate
            elif (
                isinstance(candidate, str)
                and len(candidate) <= 64
                and not any(ord(char) < 32 or ord(char) == 127 for char in candidate)
            ):
                error_code = candidate
        except json.JSONDecodeError, UnicodeDecodeError:
            pass
    return OpenRouterHTTPError(
        status_code=response.status_code,
        method=method,
        endpoint=endpoint,
        error_code=error_code,
        retry_after=_safe_header(response, "Retry-After"),
        request_id=(
            _safe_header(response, "X-Request-Id")
            or _safe_header(response, "X-OpenRouter-Request-Id")
        ),
    )


def _safe_header(response: httpx.Response, name: str) -> str | None:
    value = response.headers.get(name)
    if (
        value is None
        or len(value) > 255
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return None
    return value


def _media_type(response: httpx.Response) -> str:
    return response.headers.get("Content-Type", "").partition(";")[0].strip().casefold()


def _video_path(job_id: str) -> str:
    _require_wire_identifier("job_id", job_id)
    return f"/videos/{quote(job_id, safe='')}"


def _require_wire_identifier(name: str, value: object) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 255
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"{name} must be a bounded identifier")


def _require_header_value(name: str, value: object, *, max_length: int) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_length
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(f"{name} must be a bounded header value")


__all__ = [
    "GENERATION_ID_HEADER",
    "OPENROUTER_BASE_URL",
    "OpenRouterClient",
]
