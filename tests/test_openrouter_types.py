"""Typed OpenRouter request, routing, usage, and job contracts."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from derp.openrouter import (
    DataCollectionPolicy,
    ImageGenerationRequest,
    ImageUsage,
    MediaReference,
    MediaReferenceKind,
    ModelListQuery,
    ModelPricing,
    OpenRouterCostError,
    OpenRouterJobError,
    OpenRouterUsageError,
    ProviderPartition,
    ProviderRouting,
    ProviderSort,
    ProviderSortConfig,
    SpeechRequest,
    TimestampGranularity,
    TranscriptionRequest,
    TranscriptionResponseFormat,
    VideoFrame,
    VideoFrameType,
    VideoGenerationRequest,
    VideoJob,
    VideoJobStatus,
)


def test_provider_routing_serializes_privacy_and_passthrough_policy() -> None:
    routing = ProviderRouting(
        order=("google-vertex", "google-ai-studio"),
        allow_fallbacks=False,
        require_parameters=True,
        data_collection=DataCollectionPolicy.DENY,
        zdr=True,
        sort=ProviderSort.LATENCY,
        options={"google-vertex": {"output_config": {"effort": "low"}}},
    )

    assert routing.to_payload() == {
        "order": ["google-vertex", "google-ai-studio"],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
        "sort": "latency",
        "options": {"google-vertex": {"output_config": {"effort": "low"}}},
    }
    assert "output_config" not in repr(routing)


def test_provider_routing_serializes_structured_exacto_sort() -> None:
    routing = ProviderRouting(
        sort=ProviderSortConfig(
            by=ProviderSort.EXACTO,
            partition=ProviderPartition.NONE,
        )
    )

    assert routing.to_payload() == {
        "sort": {"by": "exacto", "partition": "none"},
    }


def test_model_query_rejects_unsupported_non_zdr_filter() -> None:
    with pytest.raises(ValidationError):
        ModelListQuery(zdr=False)


@pytest.mark.parametrize(
    "providers",
    [(), ("duplicate", "duplicate"), ("bad\nheader",)],
)
def test_provider_routing_rejects_ambiguous_or_unsafe_lists(
    providers: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError):
        ProviderRouting(order=providers)


def test_media_references_allow_https_and_image_data_only() -> None:
    remote = MediaReference(
        kind=MediaReferenceKind.VIDEO,
        url="https://cdn.example/video.mp4",
    )
    inline = MediaReference(
        kind=MediaReferenceKind.IMAGE,
        url="data:image/png;base64,AAAA",
    )

    assert remote.to_payload() == {
        "type": "video_url",
        "video_url": {"url": "https://cdn.example/video.mp4"},
    }
    assert inline.to_payload()["type"] == "image_url"
    with pytest.raises(ValidationError, match="HTTPS"):
        MediaReference(
            kind=MediaReferenceKind.AUDIO,
            url="http://cdn.example/audio.mp3",
        )


def test_image_request_omits_streaming_and_hides_prompt_from_repr() -> None:
    request = ImageGenerationRequest(
        model="provider/image-model",
        prompt="private prompt",
        input_references=(
            MediaReference(
                kind=MediaReferenceKind.IMAGE,
                url="https://cdn.example/reference.png",
            ),
        ),
    )

    payload = request.to_payload()
    assert payload["prompt"] == "private prompt"
    assert payload["input_references"] == [
        {
            "type": "image_url",
            "image_url": {"url": "https://cdn.example/reference.png"},
        }
    ]
    assert "stream" not in payload
    assert "private prompt" not in repr(request)


def test_dedicated_surfaces_reject_undocumented_routing_controls() -> None:
    private_routing = ProviderRouting(
        data_collection=DataCollectionPolicy.DENY,
        zdr=True,
    )
    selected_routing = ProviderRouting(only=("provider-a",))

    with pytest.raises(ValidationError, match="image routing does not document"):
        ImageGenerationRequest(
            model="provider/image-model",
            prompt="prompt",
            provider=private_routing,
        )
    with pytest.raises(ValidationError, match="options only"):
        SpeechRequest(
            model="provider/tts-model",
            input="text",
            voice="voice",
            provider=selected_routing,
        )


def test_transcription_payload_encodes_raw_audio_without_repr_exposure() -> None:
    request = TranscriptionRequest(
        model="provider/stt-model",
        audio=b"private audio",
        audio_format="wav",
        response_format=TranscriptionResponseFormat.VERBOSE_JSON,
        timestamp_granularities=(TimestampGranularity.WORD,),
    )

    payload = request.to_payload()
    assert payload["input_audio"] == {
        "data": "cHJpdmF0ZSBhdWRpbw==",
        "format": "wav",
    }
    assert payload["timestamp_granularities"] == ["word"]
    assert "private audio" not in repr(request)


def test_transcription_timestamps_fail_closed_outside_verbose_json() -> None:
    with pytest.raises(ValidationError, match="verbose_json"):
        TranscriptionRequest(
            model="provider/stt-model",
            audio=b"audio",
            audio_format="wav",
            timestamp_granularities=(TimestampGranularity.SEGMENT,),
        )


def test_video_request_requires_prompt_or_reference_and_unique_frames() -> None:
    with pytest.raises(ValidationError, match="prompt or reference"):
        VideoGenerationRequest(model="provider/video-model")
    with pytest.raises(ValidationError, match="positions must be unique"):
        VideoGenerationRequest(
            model="provider/video-model",
            frame_images=(
                VideoFrame(
                    frame_type=VideoFrameType.FIRST,
                    url="https://cdn.example/one.png",
                ),
                VideoFrame(
                    frame_type=VideoFrameType.FIRST,
                    url="https://cdn.example/two.png",
                ),
            ),
        )


def test_usage_and_job_requirements_raise_typed_safe_errors() -> None:
    usage = ImageUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
    failed = VideoJob(
        id="job-1",
        polling_url="/api/v1/videos/job-1",
        status=VideoJobStatus.FAILED,
        generation_id=None,
        unsigned_urls=(),
        usage=None,
        error_present=True,
    )

    with pytest.raises(OpenRouterCostError):
        usage.require_cost()
    with pytest.raises(OpenRouterJobError) as job_error:
        failed.require_completed()
    with pytest.raises(OpenRouterUsageError):
        failed.require_usage()
    assert "job-1" not in str(job_error.value)


def test_video_job_repr_hides_remote_urls() -> None:
    job = VideoJob(
        id="job-1",
        polling_url="https://openrouter.example/jobs/job-1?secret=poll",
        status=VideoJobStatus.COMPLETED,
        generation_id="generation-1",
        unsigned_urls=("https://media.example/video.mp4?secret=content",),
        usage=None,
        error_present=False,
    )

    rendered = repr(job)
    assert "secret=poll" not in rendered
    assert "secret=content" not in rendered


def test_decimal_cost_is_preserved_exactly() -> None:
    usage = ImageUsage(
        prompt_tokens=1,
        completion_tokens=2,
        total_tokens=3,
        cost=Decimal("0.000000123456"),
    )

    assert usage.require_cost() == Decimal("0.000000123456")


def test_model_pricing_accepts_documented_router_unavailable_sentinel() -> None:
    pricing = ModelPricing.model_validate({"prompt": -1, "completion": "-1"})

    assert pricing.prompt is None
    assert pricing.completion is None
