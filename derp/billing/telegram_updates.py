"""Telegram adapters for durable Stars update acknowledgement and replay."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.dispatcher.dispatcher import DEFAULT_BACKOFF_CONFIG
from aiogram.types import Update
from aiogram.utils.backoff import BackoffConfig

from derp.billing.payloads import hash_invoice_payload
from derp.billing.payment_updates import (
    PaymentReplyDisposition,
    PaymentSettlementState,
    PaymentUpdateDisposition,
    PaymentUpdateEnvelope,
    PaymentUpdateInboxService,
    PaymentUpdateKind,
    PaymentUpdateOutcome,
)
from derp.observability import report_exception

logger = logging.getLogger(__name__)


class PaymentInboxPersistenceError(RuntimeError):
    """A relevant Telegram update could not reach durable storage."""


def payment_update_from_telegram(update: Update) -> PaymentUpdateEnvelope | None:
    """Extract only allowlisted fields and immediately hash the bearer payload."""
    message = update.message
    if message is None:
        return None

    payment = message.successful_payment or message.refunded_payment
    if payment is None:
        return None

    from_user = message.from_user
    reply_chat_id = from_user.id if from_user is not None else None
    reply_language = (
        "ru"
        if from_user is not None
        and (from_user.language_code or "").lower().startswith("ru")
        else "en"
    )
    if message.successful_payment is not None:
        successful = message.successful_payment
        expiration = (
            datetime.fromtimestamp(successful.subscription_expiration_date, UTC)
            if successful.subscription_expiration_date is not None
            else None
        )
        return PaymentUpdateEnvelope(
            telegram_update_id=update.update_id,
            kind=PaymentUpdateKind.SUCCESSFUL,
            payload_token_hash=hash_invoice_payload(successful.invoice_payload),
            telegram_charge_id=successful.telegram_payment_charge_id,
            provider_charge_id=successful.provider_payment_charge_id,
            payer_telegram_id=from_user and from_user.id,
            reply_chat_id=reply_chat_id,
            reply_language=reply_language,
            currency=successful.currency,
            total_amount=successful.total_amount,
            is_recurring=bool(successful.is_recurring),
            is_first_recurring=bool(successful.is_first_recurring),
            subscription_expiration_at=expiration,
        )

    refunded = message.refunded_payment
    if refunded is None:  # pragma: no cover - payment union narrowed above
        return None
    return PaymentUpdateEnvelope(
        telegram_update_id=update.update_id,
        kind=PaymentUpdateKind.REFUNDED,
        payload_token_hash=hash_invoice_payload(refunded.invoice_payload),
        telegram_charge_id=refunded.telegram_payment_charge_id,
        provider_charge_id=refunded.provider_payment_charge_id,
        payer_telegram_id=None,
        reply_chat_id=reply_chat_id,
        reply_language=reply_language,
        currency=refunded.currency,
        total_amount=refunded.total_amount,
    )


class TelegramPaymentUpdateNotifier:
    """Send content-minimized recovery notices without replaying handler state."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def notify(
        self,
        outcome: PaymentUpdateOutcome,
    ) -> PaymentReplyDisposition:
        if outcome.reply_chat_id is None:
            return PaymentReplyDisposition.SKIPPED
        text = self._text(outcome)
        try:
            await self._bot.send_message(
                outcome.reply_chat_id,
                text,
                protect_content=True,
            )
        except Exception as exc:
            report_exception(
                "payment_update_recovery_reply_failed",
                exception=exc,
                level="warning",
                telegram_user_id=outcome.reply_chat_id,
            )
            return PaymentReplyDisposition.FAILED
        return PaymentReplyDisposition.SENT

    @staticmethod
    def _text(outcome: PaymentUpdateOutcome) -> str:
        russian = outcome.reply_language == "ru"
        if outcome.disposition is PaymentUpdateDisposition.ATTENTION:
            return (
                "Платёж требует проверки. Не платите повторно. Откройте /support."
                if russian
                else "This payment needs review. Don't pay again. Open /support."
            )
        if outcome.kind is PaymentUpdateKind.REFUNDED:
            return (
                "Возврат обработан. Баланс: /credits."
                if russian
                else "Refund processed. Check /credits."
            )
        if outcome.settlement_state is PaymentSettlementState.CLAWED_BACK:
            return (
                "Этот платёж уже возвращён. Баланс: /credits."
                if russian
                else "This payment was already refunded. Check /credits."
            )
        return (
            "Платёж обработан. Баланс: /credits."
            if russian
            else "Payment processed. Check /credits."
        )


class DurablePaymentDispatcher(Dispatcher):
    """Persist Stars updates synchronously before aiogram advances the offset."""

    def __init__(
        self,
        *args: Any,
        payment_update_inbox: PaymentUpdateInboxService,
        **kwargs: Any,
    ) -> None:
        self._payment_update_inbox = payment_update_inbox
        super().__init__(
            *args,
            payment_update_inbox=payment_update_inbox,
            **kwargs,
        )

    async def persist_payment_update(self, update: Update) -> uuid.UUID | None:
        """Return only after the relevant update is durably committed."""
        try:
            envelope = payment_update_from_telegram(update)
            if envelope is None:
                return None
            return await self._payment_update_inbox.persist(envelope)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise PaymentInboxPersistenceError(
                "payment update persistence failed"
            ) from None

    async def _polling(
        self,
        bot: Bot,
        polling_timeout: int = 30,
        handle_as_tasks: bool = True,
        backoff_config: BackoffConfig = DEFAULT_BACKOFF_CONFIG,
        allowed_updates: list[str] | None = None,
        tasks_concurrency_limit: int | None = None,
        **kwargs: Any,
    ) -> None:
        """Run aiogram's pinned polling loop with a pre-ack persistence hook."""
        user = await bot.me()
        logger.info(
            "telegram_polling_started",
            extra={"telegram.bot_id": user.id},
        )
        semaphore = (
            asyncio.Semaphore(tasks_concurrency_limit)
            if tasks_concurrency_limit is not None and handle_as_tasks
            else None
        )

        try:
            async for update in self._listen_updates(
                bot,
                polling_timeout=polling_timeout,
                backoff_config=backoff_config,
                allowed_updates=allowed_updates,
            ):
                inbox_id = await self.persist_payment_update(update)
                update_kwargs = kwargs
                if inbox_id is not None:
                    update_kwargs = {
                        **kwargs,
                        "payment_update_inbox_id": inbox_id,
                    }
                handle_update = self._process_update(
                    bot=bot,
                    update=update,
                    **update_kwargs,
                )
                if not handle_as_tasks:
                    await handle_update
                    continue
                if semaphore is not None:
                    await semaphore.acquire()
                    task = asyncio.create_task(
                        self._process_with_semaphore(handle_update, semaphore)
                    )
                else:
                    task = asyncio.create_task(handle_update)
                self._handle_update_tasks.add(task)
                task.add_done_callback(self._handle_update_tasks.discard)
        finally:
            logger.info(
                "telegram_polling_stopped",
                extra={"telegram.bot_id": user.id},
            )


__all__ = [
    "DurablePaymentDispatcher",
    "PaymentInboxPersistenceError",
    "TelegramPaymentUpdateNotifier",
    "payment_update_from_telegram",
]
