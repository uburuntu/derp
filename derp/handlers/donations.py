from __future__ import annotations

import random

from aiogram import F, Router, html
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery
from aiogram.utils.i18n import gettext as _

from ..filters.meta import MetaCommand, MetaInfo

router = Router(name="donations")


DEFAULT_STARS = 20


def _coerce_amount(arg: str | None) -> int:
    """Coerce provided arg to a positive integer; fallback to default."""
    if not arg:
        return DEFAULT_STARS
    try:
        value = int(arg)
        return value if value > 0 else DEFAULT_STARS
    except ValueError:
        return DEFAULT_STARS


@router.message(MetaCommand("donate", args=1))
async def donate_command(message: Message, meta: MetaInfo) -> None:
    """Send an invoice to accept Telegram Stars as a donation.

    Usage: /donate [stars] (default 20)
    Examples: /donate, /donate 5, #donate_25
    """
    amount = _coerce_amount(meta.arguments[0] if meta.arguments else None)

    # Build and send the invoice. For Stars, currency must be XTR.
    await message.answer_invoice(
        title=_("Support Derp"),
        description=_("Your donation keeps the lights on. Thank you!"),
        prices=[
            LabeledPrice(label=_("Donation"), amount=amount),
        ],
        payload=f"donate:{amount}",
        currency="XTR",
    )


@router.pre_checkout_query(F.invoice_payload.startswith("donate:"))
async def handle_pre_checkout(query: PreCheckoutQuery) -> None:
    """Approve all donation payments at pre-checkout stage."""
    await query.answer(ok=True)


@router.message(
    F.successful_payment, F.successful_payment.invoice_payload.startswith("donate:")
)
async def handle_successful_payment(message: Message) -> None:
    sp = message.successful_payment
    # For Star payments, total_amount is the Stars count
    stars = sp.total_amount

    def _animal_image_url(seed: str) -> tuple[str, str]:
        """Return a random (url, kind) pair for a cat or a dog image."""
        if random.choice((True, False)):
            return (f"https://cataas.com/cat?seed={seed}", "cat")
        else:
            return (f"https://placedog.net/640/420?random&seed={seed}", "dog")

    seed = sp.telegram_payment_charge_id or str(stars)
    url, kind = _animal_image_url(seed)

    donor = (
        html.quote(message.from_user.full_name) if message.from_user else _("friend")
    )
    caption = _(
        "🙏 Thank you, {name}, for donating {stars}⭐️! Here's a {kind} for you."
    ).format(name=donor, stars=stars, kind=("🐱" if kind == "cat" else "🐶"))

    try:
        await message.answer_photo(photo=url, caption=caption)
    except Exception:
        await message.answer(
            _("🙏 Thank you, {name}, for donating {stars}⭐️ to support Derp!").format(
                name=donor, stars=stars
            )
        )
