"""Stars purchases: invoice payloads, pre-checkout checks, receipts and crediting.

The invoice payload names exactly what was offered — product, credits and
whose wallet gets them — so a payment credits what the buyer saw even if the
packs change later; pre-checkout refuses anything that no longer matches the
current offer. The receipt is written before the credits, in the same
transaction, keyed by Telegram's charge ID, so a redelivered payment credits
nothing twice.
"""

from dataclasses import dataclass

from aiogram.types import SuccessfulPayment
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.billing.models import Purchase
from derp.config import Pack, Settings
from derp.money.wallets import credit, wallet

PAYLOAD_VERSION = "p1"
TARGETS = ("user", "chat")


@dataclass(frozen=True, slots=True)
class Order:
    product: str
    credits: int
    target: str  # user | chat: whose wallet is credited
    target_id: int

    @property
    def payload(self) -> str:
        return f"{PAYLOAD_VERSION}:{self.product}:{self.credits}:{self.target}:{self.target_id}"


def parse_payload(payload: str) -> Order | None:
    parts = payload.split(":")
    if len(parts) != 5 or parts[0] != PAYLOAD_VERSION or parts[3] not in TARGETS:
        return None
    try:
        return Order(parts[1], int(parts[2]), parts[3], int(parts[4]))
    except ValueError:
        return None


def find_pack(settings: Settings, product: str) -> Pack | None:
    return next((pack for pack in settings.packs if pack.id == product), None)


def refusal(
    settings: Settings, order: Order | None, *, currency: str, stars: int, payer_id: int
) -> str | None:
    """The locale key explaining why a checkout must be refused, or None to accept it."""
    pack = find_pack(settings, order.product) if order else None
    if (
        order is None
        or pack is None
        or currency != "XTR"
        or (stars, order.credits) != (pack.stars, pack.credits)
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
    purchase = Purchase(
        telegram_charge_id=charge,
        provider_charge_id=payment.provider_payment_charge_id or None,
        user_id=payer_id,
        wallet_id=target.id,
        kind="pack",
        product=order.product,
        stars=payment.total_amount,
        credits=order.credits,
        payload=payment.invoice_payload,
    )
    session.add(purchase)
    await session.flush()
    await credit(session, target.id, f"stars:{charge}", order.credits, "purchase")
    return purchase, True
