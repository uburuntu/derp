"""Telegram adapter tests for durable image operations."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from aiogram.types import CallbackQuery, PhotoSize
from pydantic_ai import DeferredToolRequests, ModelResponse

from derp.approvals import DeferredToolApprovalService
from derp.catalog import GoogleModelKey, ImageResolution, get_google_model
from derp.delivery import (
    Delivered,
    DeliveryAuthorizationError,
    DeliveryFailed,
    DeliveryResendCallback,
    DeliveryService,
    DeliveryTarget,
    DeliveryUncertain,
    ProgressStage,
    ResendCallbackAuthorization,
    ResendResult,
)
from derp.execution import Feature, plan_execution
from derp.features import (
    ImageAwaitingFunding,
    ImageDelivered,
    ImageDeliveryUncertain,
    ImageInProgress,
    ImageNotCharged,
    ImageNotChargedReason,
    ImageOperationCoordinator,
    ImageRefunded,
)
from derp.handlers.image import (
    _outcome_markup,
    _outcome_text,
    _resend_outcome,
    handle_edit,
    handle_imagine,
    resend_image_delivery,
)
from derp.operations import (
    ImageGenerateQuoteInput,
    OperationId,
    QuoteEngine,
    QuoteId,
    ReservationRejection,
)

RESEND_TOKEN = "A" * 43


def _models() -> tuple[SimpleNamespace, SimpleNamespace]:
    return (
        SimpleNamespace(id=uuid4(), telegram_id=12345),
        SimpleNamespace(id=uuid4(), telegram_id=-100_500),
    )


def _meta(*, prompt: str, target_message) -> SimpleNamespace:
    return SimpleNamespace(target_text=prompt, target_message=target_message)


@pytest.mark.asyncio
async def test_imagine_requires_prompt_before_creating_operation(make_message) -> None:
    message = make_message(text="/imagine", business_connection_id=None)
    coordinator = AsyncMock(spec=ImageOperationCoordinator)
    approvals = MagicMock(spec=DeferredToolApprovalService)

    await handle_imagine(
        message,
        _meta(prompt="", target_message=message),
        coordinator,
        approvals,
    )

    assert "Usage" in message.reply.await_args.args[0]
    coordinator.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_imagine_builds_stable_scoped_deferred_approval(
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
    approvals = MagicMock(spec=DeferredToolApprovalService)

    with patch(
        "derp.handlers.image.present_image_approvals",
        new=AsyncMock(return_value=message),
    ) as present:
        result = await handle_imagine(
            message,
            _meta(prompt="a lighthouse", target_message=message),
            coordinator,
            approvals,
            user_model,
            chat_model,
        )

    kwargs = present.await_args.kwargs
    requests = kwargs["requests"]
    assert isinstance(requests, DeferredToolRequests)
    assert len(requests.approvals) == 1
    tool_call = requests.approvals[0]
    assert tool_call.tool_name == "generate_image"
    assert tool_call.tool_call_id == "command-image"
    assert tool_call.args_as_dict(raise_if_invalid=True) == {
        "prompt": "a lighthouse",
        "style": None,
    }
    context = kwargs["context"]
    assert context.requester_id == user_model.id
    assert context.requester_telegram_id == user_model.telegram_id
    assert context.chat_id == chat_model.id
    assert context.chat_telegram_id == -100_500
    assert context.message_id == 77
    assert context.thread_id == 42
    assert context.business_connection_id == "business-1"
    assert context.source is None
    assert kwargs["image_operations"] is coordinator
    assert kwargs["approvals"] is approvals
    response = kwargs["original_history"][-1]
    assert isinstance(response, ModelResponse)
    assert (
        response.model_name
        == get_google_model(GoogleModelKey.CHAT_ECONOMY).provider_model_id
    )
    coordinator.run.assert_not_awaited()
    assert result is message


@pytest.mark.asyncio
async def test_imagine_never_executes_before_the_run_callback(
    make_message,
) -> None:
    message = make_message(
        message_id=78,
        text="/imagine a lighthouse",
        business_connection_id=None,
    )
    user_model, chat_model = _models()
    coordinator = AsyncMock(spec=ImageOperationCoordinator)
    approvals = MagicMock(spec=DeferredToolApprovalService)

    with patch(
        "derp.handlers.image.present_image_approvals",
        new=AsyncMock(return_value=message),
    ):
        await handle_imagine(
            message,
            _meta(prompt="a lighthouse", target_message=message),
            coordinator,
            approvals,
            user_model,
            chat_model,
        )

    coordinator.run.assert_not_awaited()
    message.edit_text.assert_not_awaited()
    message.delete.assert_not_awaited()


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
    approvals = MagicMock(spec=DeferredToolApprovalService)

    with patch(
        "derp.handlers.image.present_image_approvals",
        new=AsyncMock(return_value=message),
    ) as present:
        await handle_edit(
            message,
            _meta(prompt="add fog", target_message=message),
            coordinator,
            approvals,
            user_model,
            chat_model,
        )

    kwargs = present.await_args.kwargs
    tool_call = kwargs["requests"].approvals[0]
    assert tool_call.tool_name == "edit_image"
    assert tool_call.args_as_dict(raise_if_invalid=True) == {"edit_prompt": "add fog"}
    source_reference = kwargs["context"].source
    assert source_reference.file_id == "telegram-file"
    assert source_reference.file_unique_id == "stable-file"
    assert source_reference.metadata.mime_type == "image/jpeg"
    assert source_reference.metadata.file_size == 321
    coordinator.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_requires_source_image(make_message) -> None:
    message = make_message(
        text="/edit add fog",
        business_connection_id=None,
    )
    user_model, chat_model = _models()
    coordinator = AsyncMock(spec=ImageOperationCoordinator)
    approvals = MagicMock(spec=DeferredToolApprovalService)

    await handle_edit(
        message,
        _meta(prompt="add fog", target_message=message),
        coordinator,
        approvals,
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


def test_uncertain_outcome_has_compact_typed_send_again_callback() -> None:
    operation_id = OperationId.for_command(
        feature=Feature.IMAGE_GENERATE,
        chat_id=-100,
        message_id=2,
    )
    outcome = ImageDeliveryUncertain(operation_id, "network", RESEND_TOKEN)

    markup = _outcome_markup(outcome)

    assert markup is not None
    button = markup.inline_keyboard[0][0]
    assert button.text == "Send again"
    assert button.callback_data is not None
    assert len(button.callback_data.encode()) <= 64
    assert DeliveryResendCallback.unpack(button.callback_data).token == RESEND_TOKEN
    assert _outcome_markup(ImageDelivered(operation_id, (1,))) is None


@pytest.mark.parametrize(
    ("delivery_outcome", "expected_type", "expected_marker", "has_button"),
    (
        (Delivered((10,)), ImageDelivered, "Image delivered", False),
        (
            DeliveryUncertain("network"),
            ImageDeliveryUncertain,
            "Delivery uncertain",
            True,
        ),
        (
            DeliveryUncertain("attempt_in_progress_or_interrupted"),
            ImageInProgress,
            "In progress",
            False,
        ),
        (
            DeliveryFailed("TelegramRetryAfter", retryable=True),
            ImageInProgress,
            "In progress",
            False,
        ),
        (
            DeliveryFailed("TelegramBadRequest", retryable=False),
            ImageRefunded,
            "Refunded",
            False,
        ),
    ),
)
def test_resend_outcomes_render_honest_state(
    delivery_outcome,
    expected_type,
    expected_marker: str,
    has_button: bool,
) -> None:
    operation_id = OperationId.for_command(
        feature=Feature.IMAGE_GENERATE,
        chat_id=-100,
        message_id=3,
    )
    result = ResendResult(
        operation_id,
        DeliveryTarget(-100, 7, 3),
        delivery_outcome,
    )

    outcome = _resend_outcome(result, resend_token=RESEND_TOKEN)

    assert isinstance(outcome, expected_type)
    assert expected_marker in _outcome_text(outcome)
    assert (_outcome_markup(outcome) is not None) is has_button


@pytest.mark.asyncio
async def test_resend_callback_answers_before_authenticated_scoped_delivery(
    make_message,
    make_user,
) -> None:
    message = make_message(
        text="Delivery uncertain",
        chat_id=-1001,
        message_thread_id=77,
    )
    callback = MagicMock(spec=CallbackQuery)
    callback.message = message
    callback.from_user = make_user(id=12345)
    callback.answer = AsyncMock()
    operation_id = OperationId.for_command(
        feature=Feature.IMAGE_GENERATE,
        chat_id=-1001,
        message_id=4,
    )
    service = MagicMock(spec=DeliveryService)

    async def resend(authorization):
        callback.answer.assert_awaited_once_with()
        return ResendResult(
            operation_id,
            DeliveryTarget(-1001, 77, 4),
            Delivered((9001,)),
        )

    service.resend_from_callback = AsyncMock(side_effect=resend)

    await resend_image_delivery(
        callback,
        DeliveryResendCallback(token=RESEND_TOKEN),
        service,
    )

    service.resend_from_callback.assert_awaited_once_with(
        ResendCallbackAuthorization(
            RESEND_TOKEN,
            actor_user_id=12345,
            chat_id=-1001,
            thread_id=77,
        )
    )
    message.delete.assert_awaited_once_with()
    message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_resend_callback_does_not_modify_panel_for_wrong_actor(
    make_message,
    make_user,
) -> None:
    message = make_message(text="Delivery uncertain", chat_id=-1001)
    callback = MagicMock(spec=CallbackQuery)
    callback.message = message
    callback.from_user = make_user(id=999)
    callback.answer = AsyncMock()
    service = MagicMock(spec=DeliveryService)
    service.resend_from_callback = AsyncMock(
        side_effect=DeliveryAuthorizationError("invalid")
    )

    await resend_image_delivery(
        callback,
        DeliveryResendCallback(token=RESEND_TOKEN),
        service,
    )

    callback.answer.assert_awaited_once_with()
    message.edit_text.assert_not_awaited()
