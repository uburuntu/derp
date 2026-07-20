"""Telegram adapter tests for durable image operations."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, call
from uuid import uuid4

import pytest
from aiogram.types import PhotoSize

from derp.catalog import GoogleModelKey, ImageResolution
from derp.delivery import ProgressStage
from derp.execution import Feature, plan_execution
from derp.features import (
    ImageAwaitingFunding,
    ImageDelivered,
    ImageDeliveryUncertain,
    ImageEditRequest,
    ImageGenerateRequest,
    ImageInProgress,
    ImageNotCharged,
    ImageNotChargedReason,
    ImageOperationCoordinator,
    ImageRefunded,
)
from derp.handlers.image import _outcome_text, handle_edit, handle_imagine
from derp.operations import (
    ImageGenerateQuoteInput,
    OperationId,
    QuoteEngine,
    QuoteId,
    ReservationRejection,
)


def _models() -> tuple[SimpleNamespace, SimpleNamespace]:
    return SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())


def _meta(*, prompt: str, target_message) -> SimpleNamespace:
    return SimpleNamespace(target_text=prompt, target_message=target_message)


@pytest.mark.asyncio
async def test_imagine_requires_prompt_before_creating_operation(make_message) -> None:
    message = make_message(text="/imagine", business_connection_id=None)
    coordinator = AsyncMock(spec=ImageOperationCoordinator)

    await handle_imagine(
        message,
        _meta(prompt="", target_message=message),
        coordinator,
    )

    assert "Usage" in message.reply.await_args.args[0]
    coordinator.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_imagine_builds_stable_scoped_operation_and_uses_coordinator(
    make_message,
) -> None:
    message = make_message(
        message_id=77,
        text="/imagine a lighthouse",
        chat_id=-100_500,
        message_thread_id=42,
        business_connection_id="business-1",
    )
    user_model, chat_model = _models()
    coordinator = AsyncMock(spec=ImageOperationCoordinator)
    operation_id = OperationId.for_command(
        feature=Feature.IMAGE_GENERATE,
        chat_id=message.chat.id,
        message_id=message.message_id,
    )
    coordinator.run.return_value = ImageDelivered(operation_id, (9001,))

    result = await handle_imagine(
        message,
        _meta(prompt="a lighthouse", target_message=message),
        coordinator,
        user_model,
        chat_model,
    )

    invocation, plan, request = coordinator.run.await_args.args
    assert invocation.operation_id == operation_id
    assert invocation.request_key == ("telegram:command:image_generate:-100500:77")
    assert invocation.requester_id == user_model.id
    assert invocation.chat_id == chat_model.id
    assert invocation.thread_id == 42
    assert invocation.target.chat_id == -100_500
    assert invocation.target.thread_id == 42
    assert invocation.target.reply_to_message_id == 77
    assert invocation.target.business_connection_id == "business-1"
    assert invocation.input_tokens > 0
    assert plan.feature is Feature.IMAGE_GENERATE
    assert plan.model.key is GoogleModelKey.IMAGE
    assert request == ImageGenerateRequest("a lighthouse")
    assert coordinator.run.await_args.kwargs == {}
    assert message.edit_text.await_args_list == [
        call("Generating image..."),
        call("Image delivered."),
    ]
    assert result is message


@pytest.mark.asyncio
async def test_edit_passes_stable_telegram_reference_without_downloading(
    make_message,
) -> None:
    source_photo = PhotoSize(
        file_id="telegram-file",
        file_unique_id="stable-file",
        width=1024,
        height=768,
        file_size=321,
    )
    source = make_message(
        message_id=12,
        content_type="photo",
        photo=[source_photo],
        business_connection_id="business-1",
    )
    message = make_message(
        message_id=13,
        text="/edit add fog",
        chat_id=-100_700,
        reply_to_message=source,
        message_thread_id=9,
        business_connection_id="business-1",
    )
    user_model, chat_model = _models()
    coordinator = AsyncMock(spec=ImageOperationCoordinator)
    operation_id = OperationId.for_command(
        feature=Feature.IMAGE_EDIT,
        chat_id=message.chat.id,
        message_id=message.message_id,
    )
    coordinator.run.return_value = ImageDelivered(operation_id, (9002,))

    await handle_edit(
        message,
        _meta(prompt="add fog", target_message=message),
        coordinator,
        user_model,
        chat_model,
    )

    invocation, plan, request = coordinator.run.await_args.args
    assert invocation.operation_id == operation_id
    assert invocation.target.reply_to_message_id == message.message_id
    assert plan.feature is Feature.IMAGE_EDIT
    assert isinstance(request, ImageEditRequest)
    assert request.prompt == "add fog"
    assert request.source.file_id == "telegram-file"
    assert request.source.file_unique_id == "stable-file"
    assert request.source.metadata.mime_type == "image/jpeg"
    assert request.source.metadata.file_size == 321


@pytest.mark.asyncio
async def test_edit_requires_source_image(make_message) -> None:
    message = make_message(
        text="/edit add fog",
        business_connection_id=None,
    )
    user_model, chat_model = _models()
    coordinator = AsyncMock(spec=ImageOperationCoordinator)

    await handle_edit(
        message,
        _meta(prompt="add fog", target_message=message),
        coordinator,
        user_model,
        chat_model,
    )

    assert "Reply to or attach" in message.reply.await_args.args[0]
    coordinator.run.assert_not_awaited()


def test_all_image_operation_outcomes_have_concise_user_copy() -> None:
    operation_id = OperationId.for_command(
        feature=Feature.IMAGE_GENERATE,
        chat_id=-100,
        message_id=1,
    )
    plan = plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE)
    quote = QuoteEngine().quote(
        quote_id=QuoteId.new(),
        operation_id=operation_id,
        plan=plan,
        quote_input=ImageGenerateQuoteInput(10, ImageResolution.ONE_K),
        created_at=datetime.now(UTC),
    )

    expected = (
        (ImageDelivered(operation_id, (1,)), "Image delivered"),
        (
            ImageAwaitingFunding(
                operation_id,
                quote,
                ReservationRejection.INSUFFICIENT_FUNDS,
            ),
            "Funding needed",
        ),
        (
            ImageNotCharged(
                operation_id,
                ImageNotChargedReason.PROVIDER_FAILURE,
            ),
            "Not charged",
        ),
        (ImageRefunded(operation_id, "delivery_failed"), "Refunded"),
        (
            ImageDeliveryUncertain(operation_id, "network", "opaque-token"),
            "Delivery uncertain",
        ),
        (
            ImageInProgress(operation_id, ProgressStage.PREPARING),
            "In progress",
        ),
        (
            ImageInProgress(operation_id, ProgressStage.GENERATING),
            "In progress",
        ),
        (
            ImageInProgress(operation_id, ProgressStage.DELIVERING),
            "In progress",
        ),
    )

    for outcome, marker in expected:
        assert marker in _outcome_text(outcome)
