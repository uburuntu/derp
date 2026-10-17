"""Refunds: the owner refunds a charge; whatever of it is unspent is taken back.

Spent credits are not recovered and there is no debt: the wallet never goes
negative. Refunding a Derp+ cycle, by whichever path, also cancels its renewal
(``editUserStarSubscription``), or Telegram would charge again next period;
a failed cancel pings the owners, and repeating ``/refund`` retries it. Refunds
that reach Derp another way — a ``refunded_payment`` message, or only the daily
Star transaction sweep (chargebacks) — go through the same idempotent path.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import TransactionPartnerUser
from sqlalchemy import select

from derp.billing.models import Purchase, Subscription
from derp.config import Settings
from derp.db.engine import Database
from derp.money.wallets import claw_back
from derp.ops.owner import notify_owners

log = logging.getLogger("derp.billing")
PAGE = 100


@dataclass(frozen=True, slots=True)
class Refunded:
    purchase: Purchase
    clawed: int  # credits taken back (the rest was already spent)
    first: bool  # False when the refund had been applied before


async def apply_refund(db: Database, charge_id: str) -> Refunded | None:
    """Mark a purchase refunded and take back its unspent credits; None for unknown charges."""
    async with db.transaction() as session:
        purchase = await session.scalar(
            select(Purchase).where(Purchase.telegram_charge_id == charge_id).with_for_update()
        )
        if purchase is None:
            return None
        first = purchase.status != "refunded"
        clawed = await claw_back(session, f"stars:{charge_id}", f"refund:{charge_id}")
        if first:
            purchase.status, purchase.refunded_at = "refunded", datetime.now(UTC)
        if purchase.kind == "plan" and first:
            subscription = await session.scalar(
                select(Subscription).where(Subscription.payload == purchase.payload)
            )
            if subscription is not None:  # the refunded cycle is over: Derp+ can be bought again
                subscription.state = "canceled"
                subscription.period_end = min(subscription.period_end, datetime.now(UTC))
    return Refunded(purchase, clawed, first)


async def stop_renewal(db: Database, bot: Bot, settings: Settings, purchase: Purchase) -> None:
    """Cancel the renewal of the Derp+ subscription a refunded cycle belongs to; safe to repeat."""
    if purchase.kind != "plan":
        return
    async with db.session() as session:
        subscription = await session.scalar(
            select(Subscription).where(Subscription.payload == purchase.payload)
        )
    if subscription is None:
        return
    try:
        await bot.edit_user_star_subscription(
            user_id=subscription.user_id,
            telegram_payment_charge_id=subscription.first_charge_id,
            is_canceled=True,
        )
    except TelegramAPIError as error:
        log.warning("subscription renewal not canceled: %s", type(error).__name__)
        await notify_owners(
            bot,
            settings,
            f"Derp+ renewal wasn't canceled after refunding {purchase.telegram_charge_id} "
            f"({type(error).__name__}); Telegram may charge it again. "
            f"/refund {purchase.telegram_charge_id} retries the cancel.",
        )


async def refund(db: Database, bot: Bot, settings: Settings, charge_id: str) -> Refunded | None:
    """Refund a charge in Stars, then apply it; a charge Telegram already refunded is applied too.

    A Derp+ renewal is canceled every time, so repeating the command retries a cancel that failed.
    """
    async with db.session() as session:
        purchase = await session.scalar(select(Purchase).where(Purchase.telegram_charge_id == charge_id))
    if purchase is None:
        return None
    if purchase.status != "refunded":
        try:
            await bot.refund_star_payment(user_id=purchase.user_id, telegram_payment_charge_id=charge_id)
        except TelegramBadRequest as error:
            if "REFUNDED" not in str(error).upper():
                raise
    result = await apply_refund(db, charge_id)
    if result is not None:
        await stop_renewal(db, bot, settings, result.purchase)
    return result


async def reconcile_refunds(db: Database, bot: Bot, settings: Settings) -> int:
    """Apply refunds and chargebacks found among the bot's Star transactions; returns how many were new."""
    async with db.session() as session:
        paid = set(
            await session.scalars(select(Purchase.telegram_charge_id).where(Purchase.status == "paid"))
        )
    applied = 0
    offset = 0
    while paid:
        page = await bot.get_star_transactions(offset=offset, limit=PAGE)
        for transaction in page.transactions:
            # An outgoing transaction to a user with a purchase's charge id is that purchase's refund.
            if isinstance(transaction.receiver, TransactionPartnerUser) and transaction.id in paid:
                result = await apply_refund(db, transaction.id)
                if result is not None and result.first:
                    applied += 1
                    await stop_renewal(db, bot, settings, result.purchase)
                paid.discard(transaction.id)
        if len(page.transactions) < PAGE:
            break
        offset += PAGE
    return applied
