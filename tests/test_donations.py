import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.utils.i18n import I18n

from derp.handlers import donations as donations_module
from derp.handlers.donations import (
    DonationPayload,
    _coerce_amount,
    _donation_thanks,
    _invoice_retry_text,
    donate,
    handle_pre_checkout,
    handle_successful_payment,
)


def test_donation_payload_compact_json_under_limit():
    cases = [
        DonationPayload(a=500, c=-1001234567890, t=123456),
        DonationPayload(a=500, c=-1001234567890123),
        DonationPayload(a=1, c=28006241),
    ]
    for p in cases:
        s = p.model_dump_json(by_alias=True, exclude_none=True)
        assert len(s) < 128
        data = json.loads(s)
        assert data["k"] == "donate"


@pytest.mark.asyncio
async def test_donate_single_amount_builds_payload_and_answers_invoice():
    message = SimpleNamespace()
    message.chat = SimpleNamespace(id=-10042)
    message.message_thread_id = 777
    message.answer_invoice = AsyncMock()

    meta = SimpleNamespace(arguments=["25"])  # only .arguments is used

    await donate(message, meta)

    message.answer_invoice.assert_awaited_once()
    kwargs = message.answer_invoice.await_args.kwargs
    assert kwargs["currency"] == "XTR"
    payload = kwargs["payload"]
    parsed = DonationPayload.model_validate_json(payload)
    assert parsed.amount == 25
    assert parsed.chat_id == -10042
    assert parsed.thread_id == 777


@pytest.mark.asyncio
async def test_successful_payment_routes_ack_to_target_chat():
    # Arrange
    bot = MagicMock()
    bot.send_photo = AsyncMock()
    bot.send_message = AsyncMock()

    target = DonationPayload(a=50, c=-100999, t=123)
    message = SimpleNamespace()
    message.chat = SimpleNamespace(
        id=-100111
    )  # different from target to ensure routing
    message.message_thread_id = 9
    message.from_user = SimpleNamespace(full_name="Alice", id=1, username="alice")
    message.successful_payment = SimpleNamespace(
        total_amount=50,
        invoice_payload=target.model_dump_json(by_alias=True, exclude_none=True),
        telegram_payment_charge_id="tpcid",
        provider_payment_charge_id="ppcid",
    )

    # Act
    await handle_successful_payment(message, bot)

    # Assert
    bot.send_photo.assert_not_awaited()
    user_calls = [
        call
        for call in bot.send_message.await_args_list
        if call.kwargs.get("chat_id") == -100999
    ]
    assert len(user_calls) == 1
    kwargs = user_calls[0].kwargs
    assert kwargs["chat_id"] == -100999
    assert kwargs["message_thread_id"] == 123
    assert kwargs["text"] == "Thanks, Alice. Your 50 Stars are keeping Derp sharp."
    assert "reply_to_message_id" not in kwargs


@pytest.mark.asyncio
async def test_successful_payment_ack_failure_does_not_block_admin_notice():
    bot = MagicMock()
    bot.send_photo = AsyncMock()
    bot.send_message = AsyncMock(side_effect=[Exception("boom"), None])

    target = DonationPayload(a=10, c=-1001, t=None)
    message = SimpleNamespace()
    message.chat = SimpleNamespace(id=-1002, type="private", title=None)
    message.message_thread_id = None
    message.message_id = 12345
    message.from_user = SimpleNamespace(full_name="Bob", id=2, username="bob")
    message.successful_payment = SimpleNamespace(
        total_amount=10,
        invoice_payload=target.model_dump_json(by_alias=True, exclude_none=True),
        telegram_payment_charge_id="",
        provider_payment_charge_id="",
    )

    await handle_successful_payment(message, bot)

    assert bot.send_message.await_count == 2
    assert bot.send_message.await_args_list[0].kwargs["chat_id"] == -1001
    admin_id = donations_module.settings.rmbk_id
    admin_calls = [
        c
        for c in bot.send_message.await_args_list
        if (c.args and c.args[0] == admin_id) or c.kwargs.get("chat_id") == admin_id
    ]
    assert admin_calls, "expected an admin notification call"


def test_coerce_amount_various_cases():
    assert _coerce_amount(None) == 20
    assert _coerce_amount("") == 20
    assert _coerce_amount("abc") == 20
    assert _coerce_amount("0") == 20
    assert _coerce_amount("-5") == 20
    assert _coerce_amount("15") == 15


@pytest.mark.asyncio
async def test_donate_tiers_sends_three_invoices():
    message = SimpleNamespace()
    message.chat = SimpleNamespace(id=-555)
    message.message_thread_id = 42
    message.answer_invoice = AsyncMock(side_effect=[None, None, None])
    meta = SimpleNamespace(arguments=[])

    await donate(message, meta)

    assert message.answer_invoice.await_count == 3
    amounts = []
    for call in message.answer_invoice.await_args_list:
        payload = call.kwargs["payload"]
        p = DonationPayload.model_validate_json(payload)
        amounts.append(p.amount)
        assert p.chat_id == -555
        assert p.thread_id == 42
    assert amounts == [20, 200, 500]


@pytest.mark.asyncio
async def test_donate_tiers_invoice_failure_fallbacks():
    message = SimpleNamespace()
    message.chat = SimpleNamespace(id=-777)
    message.message_thread_id = 7
    message.answer_invoice = AsyncMock(side_effect=[None, Exception("x"), None])
    message.answer = AsyncMock()
    meta = SimpleNamespace(arguments=[])

    await donate(message, meta)

    # Fallback text sent once for the failing tier (second: 200)
    message.answer.assert_awaited()
    text = message.answer.await_args.args[0]
    assert text == (
        "I couldn't open the invoice for 200 Stars. You won't be charged. "
        "Use /donate 200 to try again."
    )


@pytest.mark.asyncio
async def test_pre_checkout_answers_ok_true():
    q = SimpleNamespace(
        invoice_payload='{"k":"donate"}',
        currency="XTR",
        total_amount=10,
        from_user=SimpleNamespace(id=1),
        answer=AsyncMock(),
    )
    await handle_pre_checkout(q)
    q.answer.assert_awaited_once_with(ok=True)


@pytest.mark.asyncio
async def test_successful_payment_decode_failure_falls_back_to_message_chat():
    bot = MagicMock()
    bot.send_photo = AsyncMock()
    bot.send_message = AsyncMock()

    message = SimpleNamespace()
    message.chat = SimpleNamespace(id=-991)
    message.message_thread_id = None
    message.from_user = SimpleNamespace(full_name="Eve", id=3, username="eve")
    message.successful_payment = SimpleNamespace(
        total_amount=5,
        invoice_payload="not-json",
        telegram_payment_charge_id="t",
        provider_payment_charge_id="p",
    )

    await handle_successful_payment(message, bot)
    bot.send_photo.assert_not_awaited()
    user_calls = [
        call
        for call in bot.send_message.await_args_list
        if call.kwargs.get("chat_id") == -991
    ]
    assert len(user_calls) == 1
    kwargs = user_calls[0].kwargs
    assert kwargs["chat_id"] == -991
    assert kwargs.get("message_thread_id") is None


def test_donation_copy_handles_singular_and_plural():
    assert _invoice_retry_text(1) == (
        "I couldn't open the invoice for 1 Star. You won't be charged. "
        "Use /donate 1 to try again."
    )
    assert _invoice_retry_text(2) == (
        "I couldn't open the invoice for 2 Stars. You won't be charged. "
        "Use /donate 2 to try again."
    )
    assert _donation_thanks("Alice", 1) == (
        "Thanks, Alice. Your 1 Star is keeping Derp sharp."
    )
    assert _donation_thanks("Alice", 2) == (
        "Thanks, Alice. Your 2 Stars are keeping Derp sharp."
    )


def test_translations_ru_title_description_and_labels():
    # Set i18n context to Russian
    i18n = I18n(path="derp/locales", default_locale="en", domain="messages")
    with i18n.context():
        with i18n.use_locale("ru"):
            # Provide a minimal in-memory translator for RU so the test
            # does not depend on compiled .mo freshness in the repo
            class DummyTranslations:
                def __init__(self, m):
                    self.m = m

                def gettext(self, s):
                    return self.m.get(s, s)

                def ngettext(self, s, p, n):
                    return self.m.get(s, s) if n == 1 else self.m.get(p, p)

            mapping = {
                "Support Derp": "Поддержать Derp",
                "A voluntary donation to support Derp's hosting and development.": (
                    "Добровольное пожертвование на хостинг и развитие Derp."
                ),
                "Donation": "Пожертвование",
            }
            i18n.locales["ru"] = DummyTranslations(mapping)

            title = donations_module.make_title()
            desc = donations_module.make_description()

            assert title == "Поддержать Derp"
            assert desc == "Добровольное пожертвование на хостинг и развитие Derp."
            assert i18n.gettext("Donation") == "Пожертвование"
