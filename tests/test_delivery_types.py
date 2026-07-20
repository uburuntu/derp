"""Delivery targets and Telegram send certainty stay explicit."""

from unittest.mock import MagicMock

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
)

from derp.delivery import (
    Delivered,
    DeliveryFailed,
    DeliveryTarget,
    DeliveryUncertain,
    classify_delivery_exception,
)


def test_delivery_target_preserves_topic_reply_and_business_scope() -> None:
    target = DeliveryTarget(-1001, 77, 42, "business")

    assert target.thread_id == 77
    assert target.reply_to_message_id == 42
    assert target.business_connection_id == "business"


@pytest.mark.parametrize(
    "target",
    [
        DeliveryTarget,
        lambda chat_id, *_: DeliveryTarget(chat_id, 0, None),
        lambda chat_id, *_: DeliveryTarget(chat_id, None, 0),
        lambda chat_id, *_: DeliveryTarget(chat_id, None, None, " "),
    ],
)
def test_delivery_target_rejects_invalid_coordinates(target) -> None:
    with pytest.raises(ValueError):
        target(0, None, None)


def test_network_and_unknown_failures_are_never_blindly_retried() -> None:
    method = MagicMock()

    network = classify_delivery_exception(TelegramNetworkError(method, "timeout"))
    timeout = classify_delivery_exception(TimeoutError())
    unknown = classify_delivery_exception(RuntimeError("unknown send state"))

    assert isinstance(network, DeliveryUncertain)
    assert isinstance(timeout, DeliveryUncertain)
    assert isinstance(unknown, DeliveryUncertain)


def test_server_rejections_distinguish_retryable_flood_control() -> None:
    method = MagicMock()

    retry = classify_delivery_exception(TelegramRetryAfter(method, "wait", 5))
    bad_request = classify_delivery_exception(TelegramBadRequest(method, "bad"))

    assert retry == DeliveryFailed("TelegramRetryAfter", retryable=True)
    assert bad_request == DeliveryFailed("TelegramBadRequest", retryable=False)


def test_delivered_requires_positive_acknowledged_message_ids() -> None:
    assert Delivered((1, 2)).message_ids == (1, 2)
    with pytest.raises(ValueError):
        Delivered(())
