"""Paid-media resend recovery is authenticated and never reopens billing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram.types import CallbackQuery

from derp.delivery import (
    Delivered,
    DeliveryAuthorizationError,
    DeliveryService,
    DeliveryTarget,
    DeliveryUncertain,
    PaidMediaResendCallback,
    ResendResult,
)
from derp.handlers.paid_media_delivery import (
    paid_media_resend_outcome,
    resend_paid_media_delivery,
)
from derp.operations import OperationId

TOKEN = "r" * 43


def _callback(message, user) -> CallbackQuery:
    callback = MagicMock(spec=CallbackQuery)
    callback.message = message
    callback.from_user = user
    callback.answer = AsyncMock()
    return callback


@pytest.mark.asyncio
async def test_resend_uses_exact_actor_chat_topic_and_deletes_on_delivery(
    make_message,
    make_user,
) -> None:
    message = make_message(chat_id=-100_123, message_thread_id=9)
    callback = _callback(message, make_user(id=77))
    operation_id = OperationId(uuid4())
    delivery = MagicMock(spec=DeliveryService)
    delivery.resend_from_callback = AsyncMock(
        return_value=ResendResult(
            operation_id,
            DeliveryTarget(-100_123, 9, 42),
            Delivered((501,)),
        )
    )

    await resend_paid_media_delivery(
        callback,
        PaidMediaResendCallback(token=TOKEN),
        delivery,
    )

    authorization = delivery.resend_from_callback.await_args.args[0]
    assert authorization.token == TOKEN
    assert authorization.actor_user_id == 77
    assert authorization.chat_id == -100_123
    assert authorization.thread_id == 9
    callback.answer.assert_awaited_once_with()
    message.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_resend_capability_never_mutates_control(
    make_message,
    make_user,
) -> None:
    message = make_message()
    callback = _callback(message, make_user(id=77))
    delivery = MagicMock(spec=DeliveryService)
    delivery.resend_from_callback = AsyncMock(
        side_effect=DeliveryAuthorizationError("invalid")
    )

    await resend_paid_media_delivery(
        callback,
        PaidMediaResendCallback(token=TOKEN),
        delivery,
    )

    message.delete.assert_not_awaited()
    message.edit_text.assert_not_awaited()


def test_uncertain_resend_retains_same_capability_without_charge() -> None:
    operation_id = OperationId(uuid4())
    result = ResendResult(
        operation_id,
        DeliveryTarget(-100_123, 9, 42),
        DeliveryUncertain("network_timeout"),
    )

    outcome = paid_media_resend_outcome(result, resend_token=TOKEN)

    assert outcome.operation_id == operation_id
    assert outcome.resend_token == TOKEN
    assert "r" * 43 not in repr(outcome)
