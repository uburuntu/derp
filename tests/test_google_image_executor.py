"""Pydantic AI image responses are normalized at one provider boundary."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai import BinaryContent, BinaryImage

from derp.catalog import GoogleModelKey
from derp.execution import Feature, Rejected, RejectionReason, Succeeded, plan_execution
from derp.features import (
    ImageGenerateRequest,
    MediaContent,
    PreparedImageEditRequest,
)
from derp.llm.image_executor import PydanticAIImageExecutor
from derp.media import MediaFamily


def _result(
    *,
    output: BinaryImage | str,
    images: list[BinaryImage],
):
    return SimpleNamespace(
        output=output,
        response=SimpleNamespace(images=images),
        new_messages=lambda: [],
    )


def _executor(result):
    agent = MagicMock()
    agent.run = AsyncMock(return_value=result)
    factory = MagicMock(return_value=agent)
    return PydanticAIImageExecutor(factory), factory, agent


@pytest.mark.asyncio
async def test_generate_returns_every_native_response_image() -> None:
    plan = plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE)
    first = BinaryImage(data=b"first", media_type="image/png")
    second = BinaryImage(data=b"second", media_type="image/jpeg")
    executor, factory, agent = _executor(_result(output=first, images=[first, second]))

    outcome = await executor.generate(
        plan,
        ImageGenerateRequest(prompt="a lighthouse", style="ink"),
    )

    assert isinstance(outcome, Succeeded)
    assert [(item.data, item.mime_type) for item in outcome.value.images] == [
        (b"first", "image/png"),
        (b"second", "image/jpeg"),
    ]
    factory.assert_called_once_with(plan)
    agent.run.assert_awaited_once_with("a lighthouse\n\nStyle: ink")


@pytest.mark.asyncio
async def test_generate_uses_binary_output_when_response_image_list_is_empty() -> None:
    plan = plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE)
    image = BinaryImage(data=b"image", media_type="image/webp")
    executor, _, _ = _executor(_result(output=image, images=[]))

    outcome = await executor.generate(plan, ImageGenerateRequest(prompt="portrait"))

    assert isinstance(outcome, Succeeded)
    assert outcome.value.images[0].data == b"image"


@pytest.mark.asyncio
async def test_text_only_provider_response_is_a_policy_rejection() -> None:
    plan = plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE)
    executor, _, _ = _executor(_result(output="I cannot create that", images=[]))

    outcome = await executor.generate(plan, ImageGenerateRequest(prompt="request"))

    assert outcome == Rejected(RejectionReason.POLICY)


@pytest.mark.asyncio
async def test_edit_sends_bounded_binary_content_without_side_effects() -> None:
    plan = plan_execution(Feature.IMAGE_EDIT, GoogleModelKey.IMAGE)
    image = BinaryImage(data=b"edited", media_type="image/png")
    executor, factory, agent = _executor(_result(output=image, images=[image]))
    request = PreparedImageEditRequest(
        prompt="make it brighter",
        source=MediaContent(
            family=MediaFamily.IMAGE,
            mime_type="image/jpeg",
            data=b"source",
        ),
    )

    outcome = await executor.edit(plan, request)

    assert isinstance(outcome, Succeeded)
    factory.assert_called_once_with(plan)
    prompt = agent.run.await_args.args[0]
    assert isinstance(prompt[0], BinaryContent)
    assert prompt[0].data == b"source"
    assert prompt[0].media_type == "image/jpeg"
    assert prompt[1] == "Edit this image: make it brighter"
