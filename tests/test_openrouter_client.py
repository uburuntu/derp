"""HTTP boundary tests for the direct OpenRouter transport."""

from __future__ import annotations

import base64
import json
from decimal import Decimal

import httpx
import pytest

from derp.openrouter import (
    ImageGenerationRequest,
    MediaReference,
    MediaReferenceKind,
    ModelListQuery,
    OpenRouterClient,
    OpenRouterHTTPError,
    OpenRouterJobError,
    OpenRouterResponseError,
    OpenRouterTransportError,
    ProviderRouting,
    ResponseFailureKind,
    SpeechRequest,
    SpeechResponseFormat,
    TimestampGranularity,
    TranscriptionRequest,
    TranscriptionResponseFormat,
    TransportFailureKind,
    VideoGenerationRequest,
    VideoJobStatus,
)

API_KEY = "test-openrouter-key"
APP_URL = "https://t.me/DerpBot"
APP_TITLE = "Derp"


def _client(http_client: httpx.AsyncClient) -> OpenRouterClient:
    return OpenRouterClient(
        api_key=API_KEY,
        app_url=APP_URL,
        app_title=APP_TITLE,
        http_client=http_client,
    )


def _json_response(
    payload: object,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(
        status,
        json=payload,
        headers={"Content-Type": "application/json", **(headers or {})},
    )


@pytest.mark.asyncio
async def test_shared_client_sends_auth_attribution_and_explicit_timeout() -> None:
    observed: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed
        observed = request
        return _json_response({"data": {"total_credits": 100.5, "total_usage": 25.75}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = _client(http)
        balance = await client.get_credits()
        await client.aclose()

        assert not http.is_closed
        assert balance.total_credits == Decimal("100.5")
        assert balance.total_usage == Decimal("25.75")

    assert observed is not None
    assert observed.url == "https://openrouter.ai/api/v1/credits"
    assert observed.headers["Authorization"] == f"Bearer {API_KEY}"
    assert observed.headers["HTTP-Referer"] == APP_URL
    assert observed.headers["X-OpenRouter-Title"] == APP_TITLE
    assert observed.headers["Accept"] == "application/json"
    assert observed.extensions["timeout"] == {
        "connect": 5.0,
        "read": 30.0,
        "write": 15.0,
        "pool": 5.0,
    }


@pytest.mark.asyncio
async def test_current_key_reports_inference_key_spend_and_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/key"
        return _json_response(
            {
                "data": {
                    "limit": 100,
                    "limit_remaining": 74.5,
                    "usage": 25.5,
                    "usage_daily": 1.25,
                    "usage_weekly": 8.5,
                    "usage_monthly": 25.5,
                    "is_free_tier": False,
                    "limit_reset": "monthly",
                    "label": "must-not-be-retained",
                    "creator_user_id": "must-not-be-retained",
                }
            }
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        key = await _client(http).get_current_key()

    assert key.limit == Decimal("100")
    assert key.limit_remaining == Decimal("74.5")
    assert key.usage == Decimal("25.5")
    assert "must-not-be-retained" not in repr(key)


@pytest.mark.asyncio
async def test_models_query_and_decimal_pricing_are_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {
            "limit": "25",
            "input_modalities": "text,image",
            "output_modalities": "image",
            "zdr": "true",
        }
        return _json_response(
            {
                "data": [
                    {
                        "id": "provider/image-model",
                        "canonical_slug": "provider/image-model-v1",
                        "name": "Image Model",
                        "created": 1,
                        "context_length": None,
                        "architecture": {
                            "input_modalities": ["text", "image"],
                            "output_modalities": ["image"],
                            "modality": "text+image->image",
                        },
                        "pricing": {
                            "prompt": "0.0000003",
                            "completion": "0",
                            "image_output": "0.04",
                        },
                        "supported_parameters": ["resolution"],
                        "supported_voices": None,
                    }
                ],
                "total_count": 1,
                "links": {"next": None},
            }
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        page = await _client(http).list_models(
            ModelListQuery(
                limit=25,
                input_modalities=("text", "image"),
                output_modalities=("image",),
                zdr=True,
            )
        )

    assert page.data[0].pricing.prompt == Decimal("0.0000003")
    assert page.data[0].pricing.image_output == Decimal("0.04")


@pytest.mark.asyncio
async def test_generation_metadata_preserves_actual_decimal_cost_and_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["id"] == "gen-1"
        return _json_response(
            {
                "data": {
                    "id": "gen-1",
                    "model": "provider/model",
                    "created_at": "2026-07-21T12:00:00Z",
                    "provider_name": "Provider",
                    "upstream_id": "upstream-1",
                    "total_cost": 0.00123456789,
                    "usage": 0.00123456789,
                    "upstream_inference_cost": 0.001,
                    "is_byok": False,
                    "cancelled": False,
                    "finish_reason": "stop",
                    "native_finish_reason": "STOP",
                    "tokens_prompt": 100,
                    "tokens_completion": 50,
                    "native_tokens_prompt": 100,
                    "native_tokens_completion": 50,
                    "native_tokens_cached": 25,
                    "native_tokens_reasoning": 10,
                    "num_input_audio_prompt": 0,
                    "generation_time": 1200.5,
                    "latency": 1250.5,
                }
            }
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        generation = await _client(http).get_generation("gen-1")

    assert generation.total_cost == Decimal("0.00123456789")
    assert generation.native_tokens_cached == 25
    assert generation.native_tokens_reasoning == 10


@pytest.mark.asyncio
async def test_image_request_sends_routing_and_decodes_buffered_media() -> None:
    prompt = "private image prompt"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload == {
            "model": "provider/image-model",
            "prompt": prompt,
            "input_references": [
                {
                    "type": "image_url",
                    "image_url": {"url": "https://cdn.example/reference.png"},
                }
            ],
            "provider": {
                "only": ["google-vertex"],
                "allow_fallbacks": False,
            },
        }
        return _json_response(
            {
                "created": 123,
                "data": [
                    {
                        "b64_json": base64.b64encode(b"image-bytes").decode(),
                        "media_type": "image/png",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 2,
                    "total_tokens": 3,
                    "cost": 0.04,
                },
            },
            headers={"X-Generation-Id": "gen-image-1"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await _client(http).generate_image(
            ImageGenerationRequest(
                model="provider/image-model",
                prompt=prompt,
                input_references=(
                    MediaReference(
                        kind=MediaReferenceKind.IMAGE,
                        url="https://cdn.example/reference.png",
                    ),
                ),
                provider=ProviderRouting(
                    only=("google-vertex",),
                    allow_fallbacks=False,
                ),
            )
        )

    assert result.images[0].data == b"image-bytes"
    assert result.images[0].media_type == "image/png"
    assert result.require_usage().require_cost() == Decimal("0.04")
    assert result.generation_id == "gen-image-1"
    assert "image-bytes" not in repr(result)


@pytest.mark.asyncio
async def test_speech_returns_raw_audio_and_requires_generation_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "model": "provider/tts-model",
            "input": "private speech",
            "voice": "voice-1",
            "response_format": "mp3",
        }
        return httpx.Response(
            200,
            content=b"audio-bytes",
            headers={
                "Content-Type": "audio/mpeg",
                "X-Generation-Id": "gen-speech-1",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await _client(http).synthesize_speech(
            SpeechRequest(
                model="provider/tts-model",
                input="private speech",
                voice="voice-1",
                response_format=SpeechResponseFormat.MP3,
            )
        )

    assert result.data == b"audio-bytes"
    assert result.media_type == "audio/mpeg"
    assert result.generation_id == "gen-speech-1"
    assert "audio-bytes" not in repr(result)


@pytest.mark.asyncio
async def test_speech_fails_closed_without_generation_header() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=b"audio",
            headers={"Content-Type": "audio/pcm"},
        )
    )
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(OpenRouterResponseError) as error:
            await _client(http).synthesize_speech(
                SpeechRequest(model="provider/tts", input="text", voice="voice")
            )

    assert error.value.kind is ResponseFailureKind.MISSING_GENERATION_ID


@pytest.mark.asyncio
async def test_transcription_uses_base64_json_and_returns_usage() -> None:
    audio = b"private audio bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["input_audio"] == {
            "data": base64.b64encode(audio).decode("ascii"),
            "format": "ogg",
        }
        assert "private audio bytes" not in request.content.decode()
        return _json_response(
            {
                "text": "private transcript",
                "task": "transcribe",
                "language": "en",
                "duration": 3.5,
                "segments": [
                    {
                        "id": 0,
                        "start": 0,
                        "end": 3.5,
                        "text": "private segment",
                    }
                ],
                "words": [
                    {
                        "word": "private",
                        "start": 0,
                        "end": 0.5,
                    }
                ],
                "usage": {
                    "seconds": 3.5,
                    "input_tokens": 10,
                    "output_tokens": 4,
                    "total_tokens": 14,
                    "cost": 0.0005,
                },
            },
            headers={"X-Generation-Id": "gen-stt-1"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await _client(http).transcribe(
            TranscriptionRequest(
                model="provider/stt-model",
                audio=audio,
                audio_format="ogg",
                response_format=TranscriptionResponseFormat.VERBOSE_JSON,
                timestamp_granularities=(TimestampGranularity.WORD,),
            )
        )

    assert result.text == "private transcript"
    assert result.generation_id == "gen-stt-1"
    assert result.require_usage().require_cost() == Decimal("0.0005")
    assert result.segments[0].text == "private segment"
    assert result.words[0].word == "private"
    assert "private transcript" not in repr(result)
    assert "private segment" not in repr(result)


@pytest.mark.asyncio
async def test_video_submit_poll_and_authenticated_content_download() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            assert request.url.path == "/api/v1/videos"
            return _json_response(
                {
                    "id": "job-1",
                    "polling_url": "https://untrusted.example/poll/job-1",
                    "status": "pending",
                },
                status=202,
            )
        if request.url.path == "/api/v1/videos/job-1/content":
            assert request.url.params["index"] == "0"
            return httpx.Response(
                200,
                content=b"video-bytes",
                headers={"Content-Type": "video/mp4"},
            )
        assert request.url.path == "/api/v1/videos/job-1"
        return _json_response(
            {
                "id": "job-1",
                "polling_url": "https://untrusted.example/poll/job-1",
                "status": "completed",
                "generation_id": "gen-video-1",
                "unsigned_urls": ["https://untrusted.example/signed-content"],
                "usage": {"cost": 0.25, "is_byok": False},
            }
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = _client(http)
        pending = await client.submit_video(
            VideoGenerationRequest(
                model="provider/video-model",
                prompt="private video prompt",
            )
        )
        completed = await client.get_video_job(pending.id)
        content = await client.get_video_content(completed.require_completed().id)

    assert pending.status is VideoJobStatus.PENDING
    assert completed.generation_id == "gen-video-1"
    assert completed.require_usage().require_cost() == Decimal("0.25")
    assert content.data == b"video-bytes"
    assert content.media_type == "video/mp4"
    assert all(request.url.host == "openrouter.ai" for request in requests)
    assert "signed-content" not in " ".join(str(request.url) for request in requests)
    assert "video-bytes" not in repr(content)


@pytest.mark.asyncio
async def test_failed_video_job_discards_remote_error_text() -> None:
    remote_error = "failure echoed private prompt"
    transport = httpx.MockTransport(
        lambda _request: _json_response(
            {
                "id": "job-1",
                "polling_url": "/api/v1/videos/job-1",
                "status": "failed",
                "error": remote_error,
            }
        )
    )
    async with httpx.AsyncClient(transport=transport) as http:
        job = await _client(http).get_video_job("job-1")

    assert job.error_present
    assert remote_error not in repr(job)
    with pytest.raises(OpenRouterJobError) as error:
        job.require_completed()
    assert remote_error not in str(error.value)


@pytest.mark.asyncio
async def test_post_error_is_not_retried_and_does_not_retain_body_or_secret() -> None:
    count = 0
    private_prompt = "private request prompt"

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return _json_response(
            {
                "error": {
                    "code": 529,
                    "message": f"provider overloaded while handling {private_prompt}",
                }
            },
            status=529,
            headers={"Retry-After": "2"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(OpenRouterHTTPError) as error:
            await _client(http).submit_video(
                VideoGenerationRequest(
                    model="provider/video-model",
                    prompt=private_prompt,
                )
            )

    assert count == 1
    assert error.value.status_code == 529
    assert error.value.error_code == 529
    assert error.value.retry_after == "2"
    assert error.value.retryable
    assert private_prompt not in str(error.value)
    assert API_KEY not in str(error.value)


@pytest.mark.asyncio
async def test_network_timeout_is_typed_without_request_or_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private transport detail", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(OpenRouterTransportError) as error:
            await _client(http).get_credits()

    assert error.value.kind is TransportFailureKind.TIMEOUT
    assert API_KEY not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.asyncio
async def test_oversized_response_is_rejected_before_buffering() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=b"not-read",
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(17 * 1024 * 1024),
            },
        )
    )
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(OpenRouterResponseError) as error:
            await _client(http).get_credits()

    assert error.value.kind is ResponseFailureKind.RESPONSE_TOO_LARGE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "content_type", "kind"),
    [
        (b"not-json", "application/json", ResponseFailureKind.INVALID_JSON),
        (b"{}", "application/json", ResponseFailureKind.INVALID_SCHEMA),
        (b"{}", "text/plain", ResponseFailureKind.UNEXPECTED_CONTENT_TYPE),
    ],
)
async def test_success_response_contract_failures_are_typed_and_content_free(
    content: bytes,
    content_type: str,
    kind: ResponseFailureKind,
) -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=content,
            headers={"Content-Type": content_type},
        )
    )
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(OpenRouterResponseError) as error:
            await _client(http).get_credits()

    assert error.value.kind is kind
    assert content.decode() not in str(error.value)


def test_client_rejects_unsafe_attribution_and_secret_headers() -> None:
    with pytest.raises(ValueError, match="api_key"):
        OpenRouterClient(
            api_key="secret\nheader",
            app_url=APP_URL,
            app_title=APP_TITLE,
        )
    with pytest.raises(ValueError, match="HTTPS"):
        OpenRouterClient(
            api_key=API_KEY,
            app_url="http://example.com",
            app_title=APP_TITLE,
        )
