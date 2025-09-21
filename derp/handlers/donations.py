from __future__ import annotations

import random
from typing import Literal

import logfire
from aiogram import Bot, F, Router, html
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery
from aiogram.utils.i18n import gettext as _
from aiogram.utils.i18n import lazy_gettext as __
from pydantic import BaseModel, ConfigDict, Field

from ..config import settings
from ..filters.meta import MetaCommand, MetaInfo

router = Router(name="donations")


DEFAULT_STARS = 20


# --- Randomized copy helpers -------------------------------------------------
EMOJIS = [
    "⭐️",
    "✨",
    "🚀",
    "☕️",
    "🍕",
    "💡",
    "🧠",
    "🛠️",
    "🎁",
    "🍩",
    "🌈",
    "🧃",
    "🪄",
    "🎉",
]

PURPOSES = [
    __("keep Derp caffeinated"),
    __("pay the server bills"),
    __("speed up image magic"),
    __("train the tiny gremlins"),
    __("fuel new features"),
    __("keep logs tidy and shiny"),
    __("bribe the RNG for better rolls"),
    __("teach Derp a new trick"),
    __("banish heisenbugs into the void"),
    __("keep the hamster wheel spinning"),
]

TITLES_LEFT = [
    __("Support Derp"),
    __("Power Up Derp"),
    __("Boost Derp"),
    __("Derp Fuel"),
    __("Derp Snacks"),
    __("Shiny Upgrades"),
]

TITLES_RIGHT = [
    __("Coffee Run"),
    __("Server Snacks"),
    __("Feature Juice"),
    __("Good Vibes"),
    __("Bug Zapper"),
    __("Gremlin School"),
]

DESCRIPTIONS_MAIN = [
    __("Every star helps {purpose}."),
    __("Your stars directly help us {purpose}."),
    __("Tiny bit of stardust to {purpose}."),
    __("Stars today, fewer bugs tomorrow — {purpose}."),
    __("A sprinkle of ✨ to {purpose}."),
]

DESCRIPTIONS_TAIL = [
    __("You rock."),
    __("Thank you for being awesome!"),
    __("High‑five from the team ✋"),
    __("We'll spend it wisely."),
    __("Much love from Derp HQ 💙"),
    __("Deploying instant good karma…"),
]


def _pick(seq: list[str]) -> str:
    return random.choice(seq)


def make_title() -> str:
    left = _pick(TITLES_LEFT)
    right = _pick(TITLES_RIGHT)
    emoji = _pick(EMOJIS)
    sep = " • " if random.random() < 0.5 else " — "
    return f"{emoji} {left}{sep}{right}"


def make_description() -> str:
    purpose = _pick(PURPOSES)
    main = _pick(DESCRIPTIONS_MAIN).format(purpose=purpose)
    tail = _pick(DESCRIPTIONS_TAIL)
    return f"{main} {tail}"


def _coerce_amount(arg: str | None) -> int:
    """Coerce provided arg to a positive integer; fallback to default."""
    if not arg:
        return DEFAULT_STARS
    try:
        value = int(arg)
        return value if value > 0 else DEFAULT_STARS
    except ValueError:
        return DEFAULT_STARS


class DonationPayload(BaseModel):
    """Validated payload for donation invoices and receipts (JSON)."""

    # Short aliases keep JSON compact to fit the 128-byte limit
    kind: Literal["donate"] = Field(default="donate", alias="k")
    amount: int = Field(alias="a")
    chat_id: int = Field(alias="c")
    thread_id: int | None = Field(default=None, alias="t")

    model_config = ConfigDict(populate_by_name=True)


@router.message(MetaCommand("donate", "support"))
async def donate(message: Message, meta: MetaInfo) -> None:
    """Donate via Telegram Stars.

    Usage:
    - /donate or /support → shows 3 invoices (low, 200, 500)
    - /donate 25 or #donate_25 → one invoice for the specified Stars
    """
    # If an explicit first argument is provided and not empty, treat as single amount
    if meta.arguments and (arg := meta.arguments[0].strip()):
        amount = _coerce_amount(arg)
        await message.answer_invoice(
            title=make_title(),
            description=make_description(),
            prices=[LabeledPrice(label=_("Donation"), amount=amount)],
            payload=DonationPayload(
                amount=amount,
                chat_id=message.chat.id,
                thread_id=message.message_thread_id,
            ).model_dump_json(by_alias=True, exclude_none=True),
            currency="XTR",
        )
        return

    # Price notes: 1000 Stars ≈ £15 GBP (approx; varies by region)
    # Suggest tiers: keep a low entry option (10–50), with median 200 and top 500
    low_options = [10, 20, 30, 50]
    low = random.choice(low_options)
    tiers = [low, 200, 500]

    # Randomize copy once so all three are a themed set
    title = make_title()
    description = make_description()

    # Send three invoices sequentially for user to pick
    for amount in tiers:
        try:
            await message.answer_invoice(
                title=title,
                description=description,
                prices=[LabeledPrice(label=_("Donation"), amount=amount)],
                payload=DonationPayload(
                    amount=amount,
                    chat_id=message.chat.id,
                    thread_id=message.message_thread_id,
                ).model_dump_json(by_alias=True, exclude_none=True),
                currency="XTR",
            )
        except Exception:
            # Degrade gracefully if something goes wrong with one invoice
            await message.answer(_("Use /donate {n} to donate {n}⭐️.").format(n=amount))


@router.pre_checkout_query(F.invoice_payload.contains('"k":"donate"'))
async def handle_pre_checkout(query: PreCheckoutQuery) -> None:
    """Approve donation pre-checkout promptly (under 10s)."""
    await query.answer(ok=True)
    logfire.info(
        "pre_checkout_ok",
        payload=query.invoice_payload,
        currency=query.currency,
        total_amount=query.total_amount,
        user_id=(query.from_user and query.from_user.id),
    )


@router.message(
    F.successful_payment, F.successful_payment.invoice_payload.contains('"k":"donate"')
)
async def handle_successful_payment(message: Message, bot: Bot) -> None:
    sp = message.successful_payment
    # For Star payments, total_amount is the Stars count
    stars = sp.total_amount
    # Route acks via payload (the payment update may arrive from another chat)
    try:
        payload_model = DonationPayload.model_validate_json(sp.invoice_payload)
    except Exception:
        logfire.exception("donation_payload_decode_failed", payload=sp.invoice_payload)
        payload_model = None
    target_chat_id = (payload_model and payload_model.chat_id) or message.chat.id
    target_thread_id = payload_model and payload_model.thread_id

    def _animal_image_url(seed: str) -> tuple[str, str]:
        """Return a random (url, kind) pair for a cat or a dog image."""
        if random.choice((True, False)):
            return (f"https://cataas.com/cat?seed={seed}", "cat")

        return (f"https://placedog.net/640/420?random&seed={seed}", "dog")

    seed = sp.telegram_payment_charge_id or str(stars)
    url, kind = _animal_image_url(seed)

    donor = message.from_user and html.quote(message.from_user.full_name) or _("friend")
    thanks_options = [
        _("🙏 Thank you, {name}, for donating {stars}⭐️! Here's a {kind} for you."),
        _("You absolute legend, {name}! {stars}⭐️ delivered. {kind} unlocked."),
        _("{name}, you fed the Derp! +{stars}⭐️ {kind} time."),
        _("{stars}⭐️ received — karma++ for {name}! Enjoy a {kind}."),
        _("{name}, your {stars}⭐️ keeps the servers toasty. {kind} incoming!"),
    ]
    caption = random.choice(thanks_options).format(
        name=donor, stars=stars, kind=("🐱" if kind == "cat" else "🐶")
    )

    # Acknowledge publicly where the donation was initiated (no cross-chat replies)
    try:
        await bot.send_photo(
            chat_id=target_chat_id,
            photo=url,
            caption=caption,
            message_thread_id=target_thread_id,
        )
        logfire.info(
            "donation_ack_photo",
            stars=stars,
            chat_id=target_chat_id,
            thread_id=target_thread_id,
            payload=sp.invoice_payload,
            user_id=(message.from_user and message.from_user.id),
            tpcid=sp.telegram_payment_charge_id,
            ppcid=sp.provider_payment_charge_id,
        )
    except Exception:
        logfire.exception("donation_ack_photo_failed")
        try:
            await bot.send_message(
                chat_id=target_chat_id,
                text=_(
                    "🙏 Thank you, {name}, for donating {stars}⭐️ to support Derp!"
                ).format(name=donor, stars=stars),
                message_thread_id=target_thread_id,
            )
            logfire.info(
                "donation_ack_text",
                stars=stars,
                chat_id=target_chat_id,
                thread_id=target_thread_id,
                payload=sp.invoice_payload,
                user_id=(message.from_user and message.from_user.id),
            )
        except Exception:
            logfire.exception("donation_ack_text_failed")

    # Notify admin with full details to keep updated
    try:
        chat = message.chat
        user = message.from_user
        payload = sp.invoice_payload
        report_lines = [
            "⭐️ Donation received",
            f"amount: {stars} XTR",
            f"payload: {payload}",
            f"chat: id={chat.id} type={chat.type} title={chat.title or ''}",
            f"origin_chat_id={message.chat.id}",
            f"target_chat_id={target_chat_id}",
            f"message_id: {message.message_id}",
            f"from: id={(user and user.id)} username={(user and ('@' + user.username) or '')} name={(user and user.full_name or '')}",
        ]
        if sp.telegram_payment_charge_id:
            report_lines.append(
                f"telegram_payment_charge_id: {sp.telegram_payment_charge_id}"
            )
        if sp.provider_payment_charge_id:
            report_lines.append(
                f"provider_payment_charge_id: {sp.provider_payment_charge_id}"
            )
        await bot.send_message(settings.rmbk_id, "\n".join(report_lines))
        logfire.info(
            "donation_admin_notified",
            stars=stars,
            payload=payload,
            chat_id=chat.id,
            thread_id=message.message_thread_id,
            user_id=(user and user.id),
        )
    except Exception:
        # Silent: admin notification failures should not affect user flow
        logfire.exception("donation_admin_notify_failed")
