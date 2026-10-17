"""Stars purchases: invoice payloads, pre-checkout checks, receipts and crediting.

The invoice payload names exactly what was offered — product, credits and
whose wallet gets them — so a payment credits what the buyer saw even if the
offer changes later; pre-checkout refuses anything that no longer matches the
current offer. The receipt is written before the credits, in the same
transaction, keyed by Telegram's charge ID, so a redelivered payment credits
nothing twice.

Chat pack payloads may carry a sixth part, the forum topic the thank-you post
goes to: invoice links are paid in the payer's private chat, so the payload is
the only place that knows where the purchase was made (128 bytes at most).

Derp+ payloads also carry a nonce: the payload identifies the subscription on
every renewal and state update. Each paid cycle (first payment or renewal)
credits one ``plan`` lot expiring with the cycle — no rollover.
"""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.types import SuccessfulPayment, TransactionPartnerUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.billing.models import Purchase, Subscription
from derp.config import Pack, Plan, Settings
from derp.db.engine import Database
from derp.money.wallets import credit, wallet

PACK_PAYLOAD, PLAN_PAYLOAD = "p1", "s1"
TARGETS = ("user", "chat")
TRANSACTIONS_PAGE = 100


@dataclass(frozen=True, slots=True)
class Order:
    product: str
    credits: int
    target: str  # user | chat: whose wallet is credited
    target_id: int
    nonce: str = ""  # set for Derp+: makes every subscription's payload unique
    thread_id: int = 0  # chat packs: the forum topic the thank-you post goes to (0: none)

    @property
    def kind(self) -> str:
        return "plan" if self.nonce else "pack"

    @property
    def payload(self) -> str:
        if self.nonce:
            return f"{PLAN_PAYLOAD}:{self.product}:{self.credits}:{self.target_id}:{self.nonce}"
        pack = f"{PACK_PAYLOAD}:{self.product}:{self.credits}:{self.target}:{self.target_id}"
        return f"{pack}:{self.thread_id}" if self.thread_id else pack


def plan_order(settings: Settings, user_id: int) -> Order:
    plan = settings.plan
    return Order(plan.id, plan.credits, "user", user_id, nonce=secrets.token_hex(6))


def parse_payload(payload: str) -> Order | None:
    parts = payload.split(":")
    try:
        if len(parts) in (5, 6) and parts[0] == PACK_PAYLOAD and parts[3] in TARGETS:
            thread = int(parts[5]) if len(parts) == 6 and parts[3] == "chat" else 0
            return Order(parts[1], int(parts[2]), parts[3], int(parts[4]), thread_id=thread)
        if len(parts) == 5 and parts[0] == PLAN_PAYLOAD and parts[4]:
            return Order(parts[1], int(parts[2]), "user", int(parts[3]), nonce=parts[4])
    except ValueError:
        return None
    return None


def find_pack(settings: Settings, product: str) -> Pack | None:
    return next((pack for pack in settings.packs if pack.id == product), None)


def refusal(
    settings: Settings, order: Order | None, *, currency: str, stars: int, payer_id: int
) -> str | None:
    """The locale key explaining why a checkout must be refused, or None to accept it."""
    offer: Pack | Plan | None = None
    if order is not None:
        offer = find_pack(settings, order.product) if order.kind == "pack" else None
        if order.kind == "plan" and order.product == settings.plan.id:
            offer = settings.plan
    if (
        order is None
        or offer is None
        or currency != "XTR"
        or (stars, order.credits) != (offer.stars, offer.credits)
    ):
        return "billing.stale"
    if order.target == "user" and order.target_id != payer_id:
        return "billing.not_yours"
    return None


async def record_payment(
    session: AsyncSession, order: Order, payment: SuccessfulPayment, payer_id: int
) -> tuple[Purchase, bool]:
    """Keep the receipt, then credit the target wallet. Returns the receipt and whether it is new."""
    charge = payment.telegram_payment_charge_id
    if existing := await session.scalar(select(Purchase).where(Purchase.telegram_charge_id == charge)):
        return existing, False
    target = await wallet(session, order.target, order.target_id)
    expires = None
    if order.kind == "plan":
        expires = _when(payment.subscription_expiration_date) or datetime.now(UTC) + timedelta(days=30)
        await _track_subscription(session, order, payment, payer_id, expires)
    purchase = Purchase(
        telegram_charge_id=charge,
        provider_charge_id=payment.provider_payment_charge_id or None,
        user_id=payer_id,
        wallet_id=target.id,
        kind=order.kind,
        product=order.product,
        stars=payment.total_amount,
        credits=order.credits,
        payload=payment.invoice_payload,
        is_recurring=bool(payment.is_recurring),
        expires_at=expires,
    )
    session.add(purchase)
    await session.flush()
    source = "plan" if order.kind == "plan" else "purchase"
    await credit(session, target.id, f"stars:{charge}", order.credits, source, expires_at=expires)
    target.empty_noticed_at = None  # a chat wallet topped up may be announced as empty again
    return purchase, True


async def recover_payments(db: Database, bot: Bot) -> list[Purchase]:
    """Record incoming Stars payments that have no receipt (their handler failed); returns the new receipts.

    An incoming invoice payment's transaction id is its ``telegram_payment_charge_id``, so a payment
    recorded either way is recorded once.
    """
    async with db.session() as session:
        known = set(await session.scalars(select(Purchase.telegram_charge_id)))
    recovered: list[Purchase] = []
    offset = 0
    while True:
        page = await bot.get_star_transactions(offset=offset, limit=TRANSACTIONS_PAGE)
        for transaction in page.transactions:
            source = transaction.source
            if not isinstance(source, TransactionPartnerUser) or transaction.id in known:
                continue
            order = parse_payload(source.invoice_payload or "")
            if source.transaction_type != "invoice_payment" or order is None:
                continue
            expires = None
            if source.subscription_period:
                paid_at = _when(transaction.date) or datetime.now(UTC)
                expires = int((paid_at + timedelta(seconds=source.subscription_period)).timestamp())
            payment = SuccessfulPayment(
                currency="XTR",
                total_amount=transaction.amount,
                invoice_payload=source.invoice_payload or "",
                telegram_payment_charge_id=transaction.id,
                provider_payment_charge_id="",
                subscription_expiration_date=expires,
                is_recurring=bool(source.subscription_period) or None,
            )
            async with db.transaction() as session:
                purchase, new = await record_payment(session, order, payment, source.user.id)
            if new:
                recovered.append(purchase)
        if len(page.transactions) < TRANSACTIONS_PAGE:
            return recovered
        offset += TRANSACTIONS_PAGE


def _when(value: datetime | int | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, int):
        return datetime.fromtimestamp(value, UTC)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _track_subscription(
    session: AsyncSession, order: Order, payment: SuccessfulPayment, payer_id: int, period_end: datetime
) -> None:
    charge = payment.telegram_payment_charge_id
    found = await session.scalar(select(Subscription).where(Subscription.payload == payment.invoice_payload))
    if found is None:
        session.add(
            Subscription(
                payload=payment.invoice_payload,
                user_id=payer_id,
                plan=order.product,
                first_charge_id=charge,
                last_charge_id=charge,
                period_end=period_end,
            )
        )
    else:
        found.state, found.last_charge_id, found.period_end = "active", charge, period_end


async def current_plan(
    session: AsyncSession, user_id: int, *, now: datetime | None = None
) -> Subscription | None:
    """The user's subscription still running (active, or canceled but paid up to its period end)."""
    return await session.scalar(
        select(Subscription)
        .where(
            Subscription.user_id == user_id,
            Subscription.state.in_(("active", "canceled")),
            Subscription.period_end > (now or datetime.now(UTC)),
        )
        .order_by(Subscription.period_end.desc())
        .limit(1)
    )
