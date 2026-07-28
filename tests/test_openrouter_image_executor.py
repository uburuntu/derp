"""OpenRouter dedicated image execution stays on the reviewed private route."""

from __future__ import annotations

import base64
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from derp.catalog import (
    ImageResolution,
    InferenceProvider,
    ModelRole,
)
from derp.execution import (
    ExecutionPlan,
    Failed,
    FailureReason,
    Feature,
    Rejected,
    RejectionReason,
    Succeeded,
    plan_execution,
)
from derp.features import ImageGenerateRequest, MediaContent, PreparedImageEditRequest
from derp.media import MediaFamily
from derp.openrouter import (
    OPENROUTER_IMAGE_VERTEX_ENDPOINT,
    GeneratedImage,
    ImageGenerationResult,
    ImageUsage,
    OpenRouterImageExecutor,
    OpenRouterTransportError,
    TransportFailureKind,
)


def _result(
    *,
    data: bytes = b"image-bytes",
    media_type: str | None = "image/png",
    usage: ImageUsage | None = None,
) -> ImageGenerationResult:
    return ImageGenerationResult(
        created=1,
        images=(GeneratedImage(data=data, media_type=media_type),),
        usage=usage
        or ImageUsage(
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            cost="0.067",
        ),
        generation_id="generation-1",
    )


def _executor(result: ImageGenerationResult | Exception):
    client = SimpleNamespace(generate_image=AsyncMock())
    if isinstance(result, Exception):
        client.generate_image.side_effect = result
    else:
        client.generate_image.return_value = result
    return OpenRouterImageExecutor(client), client


@pytest.mark.asyncio
async def test_generate_pins_exact_vertex_endpoint_and_model_version() -> None:
    executor, client = _executor(_result(media_type="IMAGE/WEBP; charset=binary"))
    plan = plan_execution(Feature.IMAGE_GENERATE, ModelRole.IMAGE)

    outcome = await executor.generate(
        plan,
        ImageGenerateRequest(
            prompt="a lighthouse",
            style="ink",
            resolution=ImageResolution.TWO_K,
        ),
    )

    assert isinstance(outcome, Succeeded)
    assert outcome.value.images[0].data == b"image-bytes"
    assert outcome.value.images[0].mime_type == "image/webp"
    request = client.generate_image.await_args.args[0]
    assert request.model == plan.model.provider_model_id
    assert request.prompt == "a lighthouse\n\nStyle: ink"
    assert request.n == 1
    assert request.resolution == "2K"
    assert request.output_format == "png"
    assert request.provider is not None
    assert request.provider.to_payload() == {
        "order": [OPENROUTER_IMAGE_VERTEX_ENDPOINT],
        "only": [OPENROUTER_IMAGE_VERTEX_ENDPOINT],
        "allow_fallbacks": False,
    }
    assert "data_collection" not in request.provider.to_payload()
    assert "zdr" not in request.provider.to_payload()
    assert "a lighthouse" not in repr(request)


@pytest.mark.asyncio
async def test_edit_sends_one_private_data_reference_and_maps_half_k() -> None:
    executor, client = _executor(_result(media_type=None))
    plan = plan_execution(Feature.IMAGE_EDIT, ModelRole.IMAGE)
    source = MediaContent(
        family=MediaFamily.IMAGE,
        mime_type="image/jpeg",
        data=b"source-image",
    )

    outcome = await executor.edit(
        plan,
        PreparedImageEditRequest(
            prompt="add a beacon",
            source=source,
            resolution=ImageResolution.HALF_K,
        ),
    )

    assert isinstance(outcome, Succeeded)
    assert outcome.value.images[0].mime_type == "image/png"
    request = client.generate_image.await_args.args[0]
    assert request.resolution == "512"
    assert len(request.input_references) == 1
    expected = base64.b64encode(source.data).decode("ascii")
    assert request.input_references[0].url == f"data:image/jpeg;base64,{expected}"
    assert expected not in repr(request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    [
        replace(
            plan_execution(Feature.IMAGE_GENERATE, ModelRole.IMAGE).model,
            available=False,
        ),
        replace(
            plan_execution(Feature.IMAGE_GENERATE, ModelRole.IMAGE).model,
            routing=replace(
                plan_execution(Feature.IMAGE_GENERATE, ModelRole.IMAGE).model.routing,
                zero_data_retention=False,
            ),
        ),
        replace(
            plan_execution(Feature.IMAGE_GENERATE, ModelRole.IMAGE).model,
            canonical_model_id="unreviewed-version",
        ),
    ],
)
async def test_unavailable_or_unreviewed_plan_fails_closed_without_io(model) -> None:
    executor, client = _executor(_result())
    plan = cast(
        ExecutionPlan,
        SimpleNamespace(feature=Feature.IMAGE_GENERATE, model=model),
    )

    outcome = await executor.generate(
        plan,
        ImageGenerateRequest(prompt="a lighthouse"),
    )

    assert outcome == Rejected(RejectionReason.POLICY)
    client.generate_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_provider_or_feature_is_a_programmer_error() -> None:
    executor, client = _executor(_result())
    google_plan = plan_execution(
        Feature.IMAGE_GENERATE,
        ModelRole.IMAGE,
        provider=InferenceProvider.GOOGLE,
    )
    edit_plan = plan_execution(Feature.IMAGE_EDIT, ModelRole.IMAGE)

    with pytest.raises(ValueError, match="OpenRouter plan"):
        await executor.generate(
            google_plan,
            ImageGenerateRequest(prompt="a lighthouse"),
        )
    with pytest.raises(ValueError, match="cannot execute"):
        await executor.generate(
            edit_plan,
            ImageGenerateRequest(prompt="a lighthouse"),
        )

    client.generate_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_typed_transport_failure_becomes_provider_failure() -> None:
    executor, _ = _executor(
        OpenRouterTransportError(
            kind=TransportFailureKind.TIMEOUT,
            method="POST",
            endpoint="/images",
        )
    )

    outcome = await executor.generate(
        plan_execution(Feature.IMAGE_GENERATE, ModelRole.IMAGE),
        ImageGenerateRequest(prompt="a lighthouse"),
    )

    assert outcome == Failed(FailureReason.PROVIDER_ERROR)


@pytest.mark.asyncio
async def test_invalid_media_identity_is_an_unusable_output() -> None:
    executor, _ = _executor(_result(media_type="application/octet-stream"))

    outcome = await executor.generate(
        plan_execution(Feature.IMAGE_GENERATE, ModelRole.IMAGE),
        ImageGenerateRequest(prompt="a lighthouse"),
    )

    assert outcome == Rejected(RejectionReason.UNUSABLE_OUTPUT)


@pytest.mark.asyncio
async def test_generation_identity_survives_when_image_usage_is_absent() -> None:
    result = _result()
    result = ImageGenerationResult(
        created=result.created,
        images=result.images,
        usage=None,
        generation_id=result.generation_id,
    )
    executor, _ = _executor(result)

    outcome = await executor.generate(
        plan_execution(Feature.IMAGE_GENERATE, ModelRole.IMAGE),
        ImageGenerateRequest(prompt="a lighthouse"),
    )

    assert isinstance(outcome, Succeeded)
    assert len(outcome.value.reports) == 1
    assert outcome.value.reports[0].generation_id == "generation-1"
    assert outcome.value.reports[0].tokens is None
    assert outcome.value.reports[0].actual_cost_usd is None
