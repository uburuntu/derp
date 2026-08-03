"""Google Veo operations are normalized behind a bounded provider adapter."""

from __future__ import annotations

import ast
import asyncio
import inspect
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.genai import types

from derp.catalog import GoogleModelKey, VideoResolution
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
    VideoAspectRatio,
    VideoGenerateRequest,
    VideoReferenceImage,
)
from derp.llm.video_executor import (
    MAX_GOOGLE_VIDEO_DOWNLOAD_DEADLINE_SECONDS,
    MAX_GOOGLE_VIDEO_POLL_ATTEMPTS,
    MAX_GOOGLE_VIDEO_POLL_INTERVAL_SECONDS,
    MAX_GOOGLE_VIDEO_POLLING_DEADLINE_SECONDS,
    MAX_GOOGLE_VIDEO_START_DEADLINE_SECONDS,
    GoogleVideoExecutionPolicy,
    GoogleVideoExecutor,
)


def _completed(
    *,
    video: types.Video | None = None,
    generated_count: int = 1,
    filtered_count: int | None = None,
    filtered_reasons: list[str] | None = None,
    error: dict[str, object] | None = None,
    use_result: bool = True,
) -> types.GenerateVideosOperation:
    generated = [types.GeneratedVideo(video=video) for _ in range(generated_count)]
    response = types.GenerateVideosResponse(
        generated_videos=generated,
        rai_media_filtered_count=filtered_count,
        rai_media_filtered_reasons=filtered_reasons,
    )
    kwargs = {"result" if use_result else "response": response}
    return types.GenerateVideosOperation(
        name="operations/video-1",
        done=True,
        error=error,
        **kwargs,
    )


def _pending() -> types.GenerateVideosOperation:
    return types.GenerateVideosOperation(name="operations/video-1", done=False)


def _executor(
    operation: object,
    *,
    downloaded: object = b"downloaded-mp4",
    policy: GoogleVideoExecutionPolicy | None = None,
    sleep: AsyncMock | None = None,
):
    generate_videos = AsyncMock(return_value=operation)
    poll = AsyncMock()
    download = AsyncMock(return_value=downloaded)
    aclose = AsyncMock()
    client = SimpleNamespace(
        aio=SimpleNamespace(
            models=SimpleNamespace(generate_videos=generate_videos),
            operations=SimpleNamespace(get=poll),
            files=SimpleNamespace(download=download),
            aclose=aclose,
        )
    )
    factory = MagicMock(return_value=client)
    sleep_mock = sleep or AsyncMock()
    executor = GoogleVideoExecutor(
        "api-key",
        policy=policy,
        client_factory=factory,
        sleep=sleep_mock,
    )
    return executor, factory, generate_videos, poll, download, sleep_mock, aclose


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_key",
    [GoogleModelKey.VIDEO_FAST, GoogleModelKey.VIDEO_STANDARD],
)
async def test_generation_uses_exact_model_source_and_billable_config(
    model_key: GoogleModelKey,
) -> None:
    executor, factory, generate, poll, download, _, _ = _executor(
        _completed(video=types.Video(video_bytes=b"inline", mime_type="video/mp4"))
    )
    plan = plan_execution(Feature.VIDEO_GENERATE, model_key)
    request = VideoGenerateRequest(
        prompt="Animate the lighthouse",
        duration_seconds=8,
        aspect_ratio=VideoAspectRatio.PORTRAIT,
        resolution=VideoResolution.HD_1080P,
        reference_image=VideoReferenceImage(
            data=b"reference",
            mime_type="image/png",
        ),
    )

    outcome = await executor.generate(plan, request)

    assert isinstance(outcome, Succeeded)
    assert outcome.value.data == b"inline"
    factory.assert_called_once_with(api_key="api-key")
    call = generate.await_args
    assert call.kwargs["model"] == plan.model.provider_model_id
    assert "prompt" not in call.kwargs
    assert "image" not in call.kwargs
    source = call.kwargs["source"]
    assert isinstance(source, types.GenerateVideosSource)
    assert source.prompt == "Animate the lighthouse"
    assert source.image.image_bytes == b"reference"
    assert source.image.mime_type == "image/png"
    config = call.kwargs["config"]
    assert config.number_of_videos == 1
    assert config.duration_seconds == 8
    assert config.aspect_ratio == "9:16"
    assert config.resolution == "1080p"
    poll.assert_not_awaited()
    download.assert_not_awaited()


@pytest.mark.asyncio
async def test_catalog_4k_resolution_is_forwarded_without_translation() -> None:
    executor, _, generate, _, _, _, _ = _executor(
        _completed(video=types.Video(video_bytes=b"inline"))
    )

    outcome = await executor.generate(
        plan_execution(Feature.VIDEO_GENERATE, GoogleModelKey.VIDEO_STANDARD),
        VideoGenerateRequest(
            prompt="A lighthouse",
            resolution=VideoResolution.UHD_4K,
        ),
    )

    assert isinstance(outcome, Succeeded)
    assert generate.await_args.kwargs["config"].resolution == "4k"


@pytest.mark.asyncio
async def test_pending_operation_is_polled_a_finite_number_of_times() -> None:
    completed = _completed(video=types.Video(video_bytes=b"mp4"))
    executor, _, _, poll, _, sleep, _ = _executor(_pending())
    poll.side_effect = [_pending(), completed]

    outcome = await executor.generate(
        plan_execution(Feature.VIDEO_GENERATE, GoogleModelKey.VIDEO_FAST),
        VideoGenerateRequest(prompt="A lighthouse"),
    )

    assert isinstance(outcome, Succeeded)
    assert poll.await_count == 2
    assert sleep.await_count == 2
    sleep.assert_awaited_with(5.0)


@pytest.mark.asyncio
async def test_poll_attempt_cap_prevents_open_ended_sleeping() -> None:
    policy = GoogleVideoExecutionPolicy(
        max_poll_attempts=2,
        poll_interval_seconds=0.001,
    )
    executor, _, _, poll, _, sleep, _ = _executor(_pending(), policy=policy)
    poll.return_value = _pending()

    outcome = await executor.generate(
        plan_execution(Feature.VIDEO_GENERATE, GoogleModelKey.VIDEO_FAST),
        VideoGenerateRequest(prompt="A lighthouse"),
    )

    assert outcome == Failed(FailureReason.PROVIDER_ERROR)
    assert poll.await_count == 2
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_polling_deadline_cancels_a_stalled_poll_cycle() -> None:
    started = asyncio.Event()

    async def stalled_sleep(_: float) -> None:
        started.set()
        await asyncio.sleep(60)

    policy = GoogleVideoExecutionPolicy(
        polling_deadline_seconds=0.001,
        poll_interval_seconds=0.001,
    )
    executor, _, _, poll, _, _, _ = _executor(
        _pending(),
        policy=policy,
        sleep=AsyncMock(side_effect=stalled_sleep),
    )

    outcome = await executor.generate(
        plan_execution(Feature.VIDEO_GENERATE, GoogleModelKey.VIDEO_FAST),
        VideoGenerateRequest(prompt="A lighthouse"),
    )

    assert outcome == Failed(FailureReason.PROVIDER_ERROR)
    assert started.is_set()
    poll.assert_not_awaited()


@pytest.mark.asyncio
async def test_external_cancellation_propagates_without_polling_after_cancel() -> None:
    started = asyncio.Event()
    never = asyncio.Event()

    async def cancellable_sleep(_: float) -> None:
        started.set()
        await never.wait()

    executor, _, _, poll, _, _, _ = _executor(
        _pending(),
        sleep=AsyncMock(side_effect=cancellable_sleep),
    )
    task = asyncio.create_task(
        executor.generate(
            plan_execution(Feature.VIDEO_GENERATE, GoogleModelKey.VIDEO_FAST),
            VideoGenerateRequest(prompt="A lighthouse"),
        )
    )
    await started.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    poll.assert_not_awaited()


@pytest.mark.asyncio
async def test_generation_start_deadline_is_typed_provider_failure() -> None:
    policy = GoogleVideoExecutionPolicy(start_deadline_seconds=0.001)
    executor, _, generate, _, _, _, _ = _executor(
        _pending(),
        policy=policy,
    )

    async def stalled_generation(**_: object) -> None:
        await asyncio.sleep(60)

    generate.side_effect = stalled_generation

    outcome = await executor.generate(
        plan_execution(Feature.VIDEO_GENERATE, GoogleModelKey.VIDEO_FAST),
        VideoGenerateRequest(prompt="A lighthouse"),
    )

    assert outcome == Failed(FailureReason.PROVIDER_ERROR)


@pytest.mark.asyncio
async def test_uri_video_is_downloaded_under_a_deadline() -> None:
    video = types.Video(uri="https://provider.invalid/private", mime_type="video/mp4")
    executor, _, _, _, download, _, _ = _executor(_completed(video=video))

    outcome = await executor.generate(
        plan_execution(Feature.VIDEO_GENERATE, GoogleModelKey.VIDEO_FAST),
        VideoGenerateRequest(prompt="A lighthouse"),
    )

    assert isinstance(outcome, Succeeded)
    assert outcome.value.data == b"downloaded-mp4"
    download.assert_awaited_once_with(file=video)


@pytest.mark.asyncio
async def test_download_deadline_is_typed_provider_failure() -> None:
    policy = GoogleVideoExecutionPolicy(download_deadline_seconds=0.001)
    executor, _, _, _, download, _, _ = _executor(
        _completed(video=types.Video(uri="files/private")),
        policy=policy,
    )

    async def stalled_download(**_: object) -> None:
        await asyncio.sleep(60)

    download.side_effect = stalled_download

    outcome = await executor.generate(
        plan_execution(Feature.VIDEO_GENERATE, GoogleModelKey.VIDEO_FAST),
        VideoGenerateRequest(prompt="A lighthouse"),
    )

    assert outcome == Failed(FailureReason.PROVIDER_ERROR)


@pytest.mark.asyncio
async def test_rai_filtered_empty_result_is_policy_rejection_without_reason_text() -> (
    None
):
    executor, _, _, _, _, _, _ = _executor(
        _completed(
            generated_count=0,
            filtered_count=1,
            filtered_reasons=["private policy detail"],
        )
    )

    outcome = await executor.generate(
        plan_execution(Feature.VIDEO_GENERATE, GoogleModelKey.VIDEO_FAST),
        VideoGenerateRequest(prompt="private prompt"),
    )

    assert outcome == Rejected(RejectionReason.POLICY)
    assert "private" not in repr(outcome)


@pytest.mark.asyncio
async def test_operation_error_is_content_free_provider_failure() -> None:
    executor, _, _, _, _, _, _ = _executor(
        _completed(
            video=types.Video(video_bytes=b"mp4"),
            error={"message": "private provider detail"},
        )
    )

    outcome = await executor.generate(
        plan_execution(Feature.VIDEO_GENERATE, GoogleModelKey.VIDEO_FAST),
        VideoGenerateRequest(prompt="private prompt"),
    )

    assert outcome == Failed(FailureReason.PROVIDER_ERROR)
    assert "private" not in repr(outcome)


@pytest.mark.asyncio
async def test_operation_error_stops_before_any_poll_or_sleep() -> None:
    operation = types.GenerateVideosOperation(
        name="operations/video-1",
        done=False,
        error={"message": "private provider detail"},
    )
    executor, _, _, poll, _, sleep, _ = _executor(operation)

    outcome = await executor.generate(
        plan_execution(Feature.VIDEO_GENERATE, GoogleModelKey.VIDEO_FAST),
        VideoGenerateRequest(prompt="private prompt"),
    )

    assert outcome == Failed(FailureReason.PROVIDER_ERROR)
    poll.assert_not_awaited()
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_polled_operation_error_stops_the_bounded_cycle_immediately() -> None:
    executor, _, _, poll, _, sleep, _ = _executor(_pending())
    poll.return_value = types.GenerateVideosOperation(
        name="operations/video-1",
        done=False,
        error={"message": "private provider detail"},
    )

    outcome = await executor.generate(
        plan_execution(Feature.VIDEO_GENERATE, GoogleModelKey.VIDEO_FAST),
        VideoGenerateRequest(prompt="private prompt"),
    )

    assert outcome == Failed(FailureReason.PROVIDER_ERROR)
    assert poll.await_count == 1
    assert sleep.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        SimpleNamespace(done=True),
        types.GenerateVideosOperation(name="operations/1", done=True),
        _completed(generated_count=0),
        _completed(video=types.Video(video_bytes=b"one"), generated_count=2),
        _completed(video=None),
        _completed(video=types.Video(video_bytes=b"mp4", mime_type="video/webm")),
        _completed(video=types.Video()),
        _completed(video=types.Video(video_bytes=b"")),
    ],
)
async def test_malformed_or_missing_provider_output_is_unusable(
    operation: object,
) -> None:
    executor, _, _, _, download, _, _ = _executor(operation)

    outcome = await executor.generate(
        plan_execution(Feature.VIDEO_GENERATE, GoogleModelKey.VIDEO_FAST),
        VideoGenerateRequest(prompt="A lighthouse"),
    )

    assert outcome == Rejected(RejectionReason.UNUSABLE_OUTPUT)
    download.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["generate", "poll", "download"])
async def test_provider_exceptions_are_typed_without_exception_content(
    boundary: str,
) -> None:
    operation = (
        _pending()
        if boundary == "poll"
        else _completed(video=types.Video(uri="files/private"))
    )
    executor, _, generate, poll, download, _, _ = _executor(operation)
    failure = RuntimeError("private provider response")
    if boundary == "generate":
        generate.side_effect = failure
    elif boundary == "poll":
        poll.side_effect = failure
    else:
        download.side_effect = failure

    outcome = await executor.generate(
        plan_execution(Feature.VIDEO_GENERATE, GoogleModelKey.VIDEO_FAST),
        VideoGenerateRequest(prompt="private prompt"),
    )

    assert outcome == Failed(FailureReason.PROVIDER_ERROR)
    assert "private" not in repr(outcome)


def test_google_video_policy_requires_finite_bounded_polling() -> None:
    limits = {
        "start_deadline_seconds": MAX_GOOGLE_VIDEO_START_DEADLINE_SECONDS,
        "polling_deadline_seconds": MAX_GOOGLE_VIDEO_POLLING_DEADLINE_SECONDS,
        "download_deadline_seconds": MAX_GOOGLE_VIDEO_DOWNLOAD_DEADLINE_SECONDS,
        "poll_interval_seconds": MAX_GOOGLE_VIDEO_POLL_INTERVAL_SECONDS,
    }
    for name, maximum in limits.items():
        with pytest.raises(ValueError, match="finite, positive"):
            GoogleVideoExecutionPolicy(**{name: math.inf})
        with pytest.raises(ValueError, match="at most"):
            GoogleVideoExecutionPolicy(**{name: maximum + 1})
    with pytest.raises(TypeError, match="must be an integer"):
        GoogleVideoExecutionPolicy(max_poll_attempts=True)
    with pytest.raises(ValueError, match="between 1"):
        GoogleVideoExecutionPolicy(max_poll_attempts=0)
    with pytest.raises(ValueError, match="between 1"):
        GoogleVideoExecutionPolicy(max_poll_attempts=MAX_GOOGLE_VIDEO_POLL_ATTEMPTS + 1)


def test_executor_rejects_blank_api_key_without_constructing_client() -> None:
    factory = MagicMock()

    with pytest.raises(ValueError, match="must not be blank"):
        GoogleVideoExecutor(" ", client_factory=factory)

    factory.assert_not_called()


@pytest.mark.asyncio
async def test_executor_closes_its_async_google_client() -> None:
    executor, _, _, _, _, _, aclose = _executor(
        _completed(video=types.Video(video_bytes=b"mp4"))
    )

    await executor.aclose()

    aclose.assert_awaited_once_with()


def test_new_video_boundaries_do_not_import_product_side_effect_layers() -> None:
    from derp.features import video as feature_module
    from derp.llm import video_executor as executor_module

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
