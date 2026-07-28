"""Capture policy keeps ambient history gated and sensitive events out."""

import pytest
from aiogram.types import Contact, MessageEntity, SuccessfulPayment

from derp.history.policy import capture_kind_for_message
from derp.history.snapshot import CaptureKind


def test_private_and_explicit_group_messages_are_captured(make_message) -> None:
    private = make_message(text="hello", chat_type="private")
    command = make_message(text="/derp hello", chat_type="supergroup")
    mention = make_message(text="hey @DerpRobot", chat_type="supergroup")
    name = make_message(text="derp what is this", chat_type="supergroup")

    for message in (private, command, mention, name):
        assert (
            capture_kind_for_message(
                message,
                ambient_enabled=False,
                bot_id=99,
                bot_username="DerpRobot",
            )
            is CaptureKind.EXPLICIT
        )


@pytest.mark.parametrize(
    "text",
    [
        "/operator",
        "/ops@DerpRobot",
        "/context",
        "/debug_buy",
        "/debug_refund sensitive-charge-id",
        "/debug_refund\nsensitive-charge-id",
        "/dcredits 100",
        "/support",
        "/paysupport@DerpRobot",
        "/terms",
    ],
)
def test_control_plane_commands_are_never_conversation_history(
    make_message,
    text: str,
) -> None:
    for chat_type in ("private", "supergroup"):
        message = make_message(text=text, chat_type=chat_type)
        assert (
            capture_kind_for_message(
                message,
                ambient_enabled=True,
                bot_id=99,
                bot_username="DerpRobot",
            )
            is None
        )


def test_operator_command_prefixes_do_not_hide_conversation(make_message) -> None:
    message = make_message(text="/operatorial", chat_type="private")

    assert (
        capture_kind_for_message(
            message,
            ambient_enabled=False,
            bot_id=99,
            bot_username="DerpRobot",
        )
        is CaptureKind.EXPLICIT
    )


def test_ambient_group_capture_is_fail_closed(make_message) -> None:
    message = make_message(text="ordinary room message", chat_type="supergroup")

    assert (
        capture_kind_for_message(
            message,
            ambient_enabled=False,
            bot_id=99,
            bot_username="DerpRobot",
        )
        is None
    )
    assert (
        capture_kind_for_message(
            message,
            ambient_enabled=True,
            bot_id=99,
            bot_username="DerpRobot",
        )
        is CaptureKind.AMBIENT
    )


def test_replies_to_bot_are_explicit(make_message, make_user) -> None:
    reply = make_message(text="prior")
    reply.from_user = make_user(id=99, is_bot=True)
    message = make_message(
        text="continue",
        chat_type="supergroup",
        reply_to_message=reply,
    )

    assert (
        capture_kind_for_message(
            message,
            ambient_enabled=False,
            bot_id=99,
            bot_username="DerpRobot",
        )
        is CaptureKind.EXPLICIT
    )


def test_username_prefix_is_not_an_explicit_invocation(make_message) -> None:
    message = make_message(
        text="please ask @DerpRobotFake instead",
        chat_type="supergroup",
    )

    assert (
        capture_kind_for_message(
            message,
            ambient_enabled=False,
            bot_id=99,
            bot_username="DerpRobot",
        )
        is None
    )


def test_exact_mention_entity_uses_telegram_utf16_offsets(make_message) -> None:
    message = make_message(
        text="👋 @DerpRobot help",
        chat_type="supergroup",
        entities=[MessageEntity(type="mention", offset=3, length=10)],
    )

    assert (
        capture_kind_for_message(
            message,
            ambient_enabled=False,
            bot_id=99,
            bot_username="derprobot",
        )
        is CaptureKind.EXPLICIT
    )


def test_sensitive_and_service_payloads_are_never_conversation_history(
    make_message,
) -> None:
    payment = make_message(text=None, content_type="successful_payment")
    payment.successful_payment = SuccessfulPayment(
        currency="XTR",
        total_amount=10,
        invoice_payload="secret",
        telegram_payment_charge_id="charge",
        provider_payment_charge_id="provider",
    )
    contact = make_message(text=None, content_type="contact")
    contact.contact = Contact(phone_number="secret", first_name="Private")
    service = make_message(text=None, content_type="new_chat_title")
    service.new_chat_title = "Renamed"

    for message in (payment, contact, service):
        assert (
            capture_kind_for_message(
                message,
                ambient_enabled=True,
                bot_id=99,
                bot_username="DerpRobot",
            )
            is None
        )
