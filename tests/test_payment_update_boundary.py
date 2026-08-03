"""Unit contracts for pre-ack Telegram Stars update persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aiogram.types import (
    Chat,
    Message,
    RefundedPayment,
    SuccessfulPayment,
    Update,
    User,
)

from derp.billing.payloads import hash_invoice_payload
from derp.billing.payment_updates import (
    PaymentReplyDisposition,
    PaymentSettlementState,
    PaymentUpdateDisposition,
    PaymentUpdateKind,
    PaymentUpdateOutcome,
)
from derp.billing.telegram_updates import (
    DurablePaymentDispatcher,
    PaymentInboxPersistenceError,
    TelegramPaymentUpdateNotifier,
    payment_update_from_telegram,
)

PAYLOAD = "dpi1_unit-test-capability"


def _payment_update(
    *,
    update_id: int = 50,
    refunded: bool = False,
    payload: str = PAYLOAD,
) -> Update:
    payment = (
        RefundedPayment(
            total_amount=50,
            invoice_payload=payload,
            telegram_payment_charge_id="telegram-charge",
            provider_payment_charge_id="provider-charge",
        )
        if refunded
        else SuccessfulPayment(
            currency="XTR",
            total_amount=50,
            invoice_payload=payload,
            telegram_payment_charge_id="telegram-charge",
            provider_payment_charge_id="provider-charge",
        )
    )
    return Update(
        update_id=update_id,
        message=Message(
            message_id=10,
            date=datetime(2026, 7, 28, tzinfo=UTC),
            chat=Chat(id=123, type="private"),
            from_user=User(
                id=123,
                is_bot=False,
                first_name="Buyer",
                language_code="ru-RU",
            ),
            refunded_payment=payment if refunded else None,
            successful_payment=None if refunded else payment,
        ),
    )


def test_extractor_hashes_payload_and_keeps_only_allowlisted_fields() -> None:
    envelope = payment_update_from_telegram(_payment_update())

    assert envelope is not None
    assert envelope.kind is PaymentUpdateKind.SUCCESSFUL
    assert envelope.payload_token_hash == hash_invoice_payload(PAYLOAD)
    assert PAYLOAD not in repr(envelope)
    assert not hasattr(envelope, "invoice_payload")
    assert envelope.payer_telegram_id == 123
    assert envelope.reply_language == "ru"


def test_extractor_maps_refund_without_payer_or_plaintext_payload() -> None:
    envelope = payment_update_from_telegram(_payment_update(refunded=True))

    assert envelope is not None
    assert envelope.kind is PaymentUpdateKind.REFUNDED
    assert envelope.payer_telegram_id is None
    assert envelope.payload_token_hash == hash_invoice_payload(PAYLOAD)
    assert PAYLOAD not in repr(envelope)


def test_extractor_quarantines_unknown_payloads_behind_the_durable_boundary() -> None:
    payload = '{"k":"legacy"}'

    envelope = payment_update_from_telegram(_payment_update(payload=payload))

    assert envelope is not None
    assert envelope.payload_token_hash == hash_invoice_payload(payload)
    assert payload not in repr(envelope)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en", "This payment was already refunded. Check /credits."),
        ("ru", "Этот платёж уже возвращён. Баланс: /credits."),
    ],
)
async def test_recovery_notifier_reports_clawed_back_payment(
    language: str,
    expected: str,
) -> None:
    bot = SimpleNamespace(send_message=AsyncMock())
    notifier = TelegramPaymentUpdateNotifier(bot)
    outcome = PaymentUpdateOutcome(
        inbox_id=UUID(int=1),
        kind=PaymentUpdateKind.SUCCESSFUL,
        disposition=PaymentUpdateDisposition.SETTLED,
        reply_chat_id=123,
        reply_language=language,
        lease_token=UUID(int=2),
        settlement_state=PaymentSettlementState.CLAWED_BACK,
    )

    result = await notifier.notify(outcome)

    assert result is PaymentReplyDisposition.SENT
    bot.send_message.assert_awaited_once_with(123, expected, protect_content=True)


class _ProbeDispatcher(DurablePaymentDispatcher):
    updates: tuple[Update, ...] = ()
    events: list[str] = []

    async def _listen_updates(self, *args, **kwargs):
        del args, kwargs
        for update in self.updates:
            yield update
            self.events.append("offset_advanced")

    async def _process_update(self, *args, **kwargs) -> bool:
        del args, kwargs
        self.events.append("handler_scheduled")
        return True


@pytest.mark.asyncio
async def test_polling_persists_payment_before_processing_and_offset_advance() -> None:
    service = SimpleNamespace(persist=AsyncMock(return_value="inbox-id"))
    dispatcher = _ProbeDispatcher(payment_update_inbox=service)
    dispatcher.updates = (_payment_update(),)
    dispatcher.events = []
    bot = SimpleNamespace(me=AsyncMock(return_value=SimpleNamespace(id=999)))

    await dispatcher._polling(bot, handle_as_tasks=False)

    service.persist.assert_awaited_once()
    assert dispatcher.events == ["handler_scheduled", "offset_advanced"]


@pytest.mark.asyncio
async def test_persistence_failure_stops_polling_before_offset_advance() -> None:
    service = SimpleNamespace(persist=AsyncMock(side_effect=OSError("database down")))
    dispatcher = _ProbeDispatcher(payment_update_inbox=service)
    dispatcher.updates = (_payment_update(),)
    dispatcher.events = []
    bot = SimpleNamespace(me=AsyncMock(return_value=SimpleNamespace(id=999)))

    with pytest.raises(
        PaymentInboxPersistenceError,
        match="payment update persistence failed",
    ):
        await dispatcher._polling(bot, handle_as_tasks=False)

    assert dispatcher.events == []
