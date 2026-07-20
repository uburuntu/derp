"""Contracts for the provider-neutral image feature boundary."""

from __future__ import annotations

import asyncio
import math
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from derp.catalog import GoogleModelKey, ImageResolution
from derp.execution import (
    Failed,
    FailureReason,
    Feature,
    Rejected,
    RejectionReason,
    Succeeded,
    plan_execution,
)
from derp.features.image import (
    MAX_IMAGE_OUTPUT_BYTES,
    MAX_IMAGE_PROMPT_CHARS,
    ImageEditRequest,
    ImageExecutionPolicy,
    ImageFeatureService,
    ImageGenerateRequest,
    ImageOutput,
    PreparedImageEditRequest,
)
from derp.features.types import MediaContent
from derp.media.types import MediaFamily, MediaMetadata, MediaReference
from derp.operations import ImageEditQuoteInput, ImageGenerateQuoteInput


def _image(data: bytes = b"image", mime_type: str = "image/png") -> MediaContent:
    return MediaContent(
        family=MediaFamily.IMAGE,
        mime_type=mime_type,
        data=data,
    )


def _reference(
    *,
    mime_type: str = "image/png",
    file_size: int | None = 5,
) -> MediaReference:
    return MediaReference(
        file_id="downloadable",
        file_unique_id="stable",
        metadata=MediaMetadata(mime_type=mime_type, file_size=file_size),
    )


def test_image_dtos_are_normalized_frozen_and_provider_neutral() -> None:
    request = ImageGenerateRequest(prompt="  a lighthouse  ", style="  ink  ")
    content = _image(mime_type="IMAGE/PNG; charset=binary")
    output = ImageOutput(images=(content,))

    assert request.prompt == "a lighthouse"
    assert request.style == "ink"
    assert request.resolution is ImageResolution.ONE_K
    assert content.mime_type == "image/png"
    assert content.size_bytes == 5
    assert output.images == (content,)
    with pytest.raises(FrozenInstanceError):
        request.prompt = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError, match="immutable tuple"):
        ImageOutput(images=[content])  # type: ignore[arg-type]


def test_image_request_resolution_is_shared_with_quote_and_prepared_edit() -> None:
    generate = ImageGenerateRequest(
        prompt="a lighthouse",
        resolution=ImageResolution.TWO_K,
    )
    edit = ImageEditRequest(
        prompt="add a beacon",
        source=_reference(),
        resolution=ImageResolution.FOUR_K,
    )
    quote_input = ImageGenerateQuoteInput(
        input_tokens=100,
        resolution=generate.resolution,
    )
    edit_quote_input = ImageEditQuoteInput(
        input_tokens=100,
        resolution=edit.resolution,
    )

    assert quote_input.resolution is generate.resolution is ImageResolution.TWO_K
    assert edit_quote_input.resolution is edit.resolution is ImageResolution.FOUR_K


@pytest.mark.parametrize(
    "request_factory",
    [
        lambda: ImageGenerateRequest(
            prompt="a lighthouse",
            resolution="1K",  # type: ignore[arg-type]
        ),
        lambda: ImageEditRequest(
            prompt="add a beacon",
            source=_reference(),
            resolution="1K",  # type: ignore[arg-type]
        ),
    ],
)
def test_image_requests_require_typed_resolution(request_factory) -> None:
    with pytest.raises(TypeError, match="ImageResolution"):
        request_factory()


@pytest.mark.parametrize(
    "request_factory",
    [
        lambda: ImageGenerateRequest(prompt="  "),
        lambda: ImageGenerateRequest(prompt="x" * (MAX_IMAGE_PROMPT_CHARS + 1)),
        lambda: ImageEditRequest(prompt="", source=_reference()),
    ],
)
def test_image_requests_reject_invalid_prompts(request_factory) -> None:
    with pytest.raises(ValueError):
        request_factory()


def test_media_content_rejects_mutable_empty_or_mismatched_data() -> None:
    with pytest.raises(TypeError, match="immutable bytes"):
        MediaContent(  # type: ignore[arg-type]
            family=MediaFamily.IMAGE,
            mime_type="image/png",
            data=bytearray(b"image"),
        )
    with pytest.raises(ValueError, match="must not be empty"):
        _image(b"")
    with pytest.raises(ValueError, match="match its family"):
        MediaContent(
            family=MediaFamily.IMAGE,
            mime_type="audio/ogg",
            data=b"audio",
        )


@pytest.mark.asyncio
async def test_generate_passes_exact_plan_and_request_to_executor() -> None:
    plan = plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE)
    request = ImageGenerateRequest(prompt="a lighthouse")
    success = Succeeded(ImageOutput(images=(_image(),)))
    executor = SimpleNamespace(
        generate=AsyncMock(return_value=success),
        edit=AsyncMock(),
    )

    result = await ImageFeatureService(executor).generate(plan, request)

    assert result is success
    executor.generate.assert_awaited_once_with(plan, request)
    executor.edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_rejects_plan_for_another_image_capability() -> None:
    executor = SimpleNamespace(generate=AsyncMock(), edit=AsyncMock())
    service = ImageFeatureService(executor)
    edit_plan = plan_execution(Feature.IMAGE_EDIT, GoogleModelKey.IMAGE)

    with pytest.raises(ValueError, match="cannot execute image_generate"):
        await service.generate(edit_plan, ImageGenerateRequest(prompt="a lighthouse"))

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

        async def edit(self, plan, request):  # pragma: no cover - protocol stub
            raise AssertionError("not called")

    executor = SlowExecutor()
    service = ImageFeatureService(
        executor,
        policy=ImageExecutionPolicy(provider_deadline_seconds=0.001),
    )

    result = await service.generate(
        plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE),
        ImageGenerateRequest(prompt="a lighthouse"),
    )

    assert result == Failed(FailureReason.PROVIDER_ERROR)
    assert executor.cancelled


@pytest.mark.asyncio
async def test_provider_exception_and_rejection_remain_typed() -> None:
    plan = plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE)
    request = ImageGenerateRequest(prompt="a lighthouse")
    rejected = Rejected(RejectionReason.POLICY)
    executor = SimpleNamespace(
        generate=AsyncMock(return_value=rejected),
        edit=AsyncMock(),
    )
    service = ImageFeatureService(executor)

    assert await service.generate(plan, request) is rejected
    executor.generate.side_effect = RuntimeError("provider detail")
    assert await service.generate(plan, request) == Failed(FailureReason.PROVIDER_ERROR)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output",
    [
        ImageOutput(images=(_image(mime_type="image/gif"),)),
        ImageOutput(images=(_image(b"1234"),)),
        ImageOutput(images=(_image(b"1"), _image(b"2"))),
    ],
)
async def test_unusable_provider_output_is_rejected_without_side_effects(
    output: ImageOutput,
) -> None:
    plan = plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE)
    executor = SimpleNamespace(
        generate=AsyncMock(return_value=Succeeded(output)),
        edit=AsyncMock(),
    )
    policy = ImageExecutionPolicy(
        max_output_bytes=3,
        max_output_images=1,
    )

    result = await ImageFeatureService(executor, policy=policy).generate(
        plan,
        ImageGenerateRequest(prompt="a lighthouse"),
    )

    assert result == Rejected(RejectionReason.UNUSABLE_OUTPUT)


@pytest.mark.asyncio
async def test_edit_hydrates_bounded_reference_before_provider_execution() -> None:
    reference = _reference(file_size=5)
    loader = SimpleNamespace(load=AsyncMock(return_value=b"image"))
    success = Succeeded(ImageOutput(images=(_image(b"edited"),)))
    executor = SimpleNamespace(
        generate=AsyncMock(),
        edit=AsyncMock(return_value=success),
    )
    service = ImageFeatureService(
        executor,
        source_loader=loader,
        policy=ImageExecutionPolicy(max_source_bytes=5),
    )
    plan = plan_execution(Feature.IMAGE_EDIT, GoogleModelKey.IMAGE)

    result = await service.edit(
        plan,
        ImageEditRequest(
            prompt="add a beacon",
            source=reference,
            resolution=ImageResolution.FOUR_K,
        ),
    )

    assert result is success
    loader.load.assert_awaited_once_with(reference)
    prepared = executor.edit.await_args.args[1]
    assert isinstance(prepared, PreparedImageEditRequest)
    assert prepared.prompt == "add a beacon"
    assert prepared.resolution is ImageResolution.FOUR_K
    assert prepared.source.data == b"image"
    assert prepared.source.mime_type == "image/png"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reference", "source_bytes"),
    [
        (_reference(mime_type="image/gif"), b"image"),
        (_reference(file_size=6), b"image"),
        (_reference(file_size=None), b"123456"),
        (_reference(file_size=None), b""),
    ],
)
async def test_edit_rejects_unsupported_or_oversized_source_before_provider(
    reference: MediaReference,
    source_bytes: bytes,
) -> None:
    loader = SimpleNamespace(load=AsyncMock(return_value=source_bytes))
    executor = SimpleNamespace(generate=AsyncMock(), edit=AsyncMock())
    service = ImageFeatureService(
        executor,
        source_loader=loader,
        policy=ImageExecutionPolicy(
            max_source_bytes=5,
            allowed_input_mime_types=frozenset({"image/png"}),
        ),
    )

    result = await service.edit(
        plan_execution(Feature.IMAGE_EDIT, GoogleModelKey.IMAGE),
        ImageEditRequest(prompt="add a beacon", source=reference),
    )

    assert result == Rejected(RejectionReason.INVALID_INPUT)
    executor.edit.assert_not_awaited()
    if reference.metadata.mime_type != "image/png" or reference.metadata.file_size == 6:
        loader.load.assert_not_awaited()


def test_policy_rejects_unbounded_or_non_image_configuration() -> None:
    policy = ImageExecutionPolicy()

    assert policy.max_output_bytes == MAX_IMAGE_OUTPUT_BYTES
    assert policy.max_output_images == 1
    with pytest.raises(TypeError, match="must be an integer"):
        ImageExecutionPolicy(max_output_bytes=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="finite and positive"):
        ImageExecutionPolicy(provider_deadline_seconds=math.inf)
    with pytest.raises(ValueError, match="must not exceed"):
        ImageExecutionPolicy(max_output_bytes=MAX_IMAGE_OUTPUT_BYTES + 1)
    with pytest.raises(ValueError, match="must be 1 for image v1"):
        ImageExecutionPolicy(max_output_images=2)
    with pytest.raises(ValueError, match="image MIME"):
        ImageExecutionPolicy(
            allowed_output_mime_types=frozenset({"audio/ogg"}),
        )
