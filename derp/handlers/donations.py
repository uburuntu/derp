from __future__ import annotations

from typing import Literal

import logfire
from aiogram import Bot, F, Router, html
from aiogram.types import LabeledPrice, Message, PreCheckoutQuery
from aiogram.utils.i18n import gettext as _
from pydantic import BaseModel, ConfigDict, Field

from ..config import settings
from ..filters.meta import MetaCommand, MetaInfo
from ..observability import report_exception

router = Router(name="donations")


DEFAULT_STARS = 20
DONATION_TIERS = (DEFAULT_STARS, 200, 500)


def make_title() -> str:
    return _("Support Derp")


def make_description() -> str:
    return _("A voluntary donation to support Derp's hosting and development.")


def _invoice_retry_text(stars: int) -> str:
    return _(
        "I couldn't open the invoice for {stars} Star. "
        "You won't be charged. Use /donate {stars} to try again.",
        "I couldn't open the invoice for {stars} Stars. "
        "You won't be charged. Use /donate {stars} to try again.",
        stars,
    ).format(stars=stars)


def _donation_thanks(donor: str, stars: int) -> str:
    return _(
        "Thanks, {name}. Your {stars} Star is keeping Derp sharp.",
        "Thanks, {name}. Your {stars} Stars are keeping Derp sharp.",
        stars,
    ).format(name=donor, stars=stars)


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

    # Send three invoices sequentially for user to pick
    for amount in DONATION_TIERS:
        try:
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
        except Exception:
            # Degrade gracefully if something goes wrong with one invoice
            await message.answer(_invoice_retry_text(amount))


@router.pre_checkout_query(F.invoice_payload.contains('"k":"donate"'))
async def handle_pre_checkout(query: PreCheckoutQuery) -> None:
    """Approve donation pre-checkout promptly (under 10s)."""
    await query.answer(ok=True)
    logfire.info(
        "pre_checkout_ok",
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
        report_exception("donation_payload_decode_failed")
        payload_model = None
    target_chat_id = (payload_model and payload_model.chat_id) or message.chat.id
    target_thread_id = payload_model and payload_model.thread_id

    donor = message.from_user and html.quote(message.from_user.full_name) or _("friend")
    thanks = _donation_thanks(donor, stars)

    # Acknowledge publicly where the donation was initiated (no cross-chat replies)
    try:
        await bot.send_message(
            chat_id=target_chat_id,
            text=thanks,
            message_thread_id=target_thread_id,
        )
        logfire.info(
            "donation_ack_sent",
            stars=stars,
            chat_id=target_chat_id,
            thread_id=target_thread_id,
            user_id=(message.from_user and message.from_user.id),
        )
    except Exception as exc:
        report_exception(
            "donation_ack_failed",
            exception=exc,
            level="warning",
        )

    if not settings.operator_ids:
        return

    chat = message.chat
    user = message.from_user
    payload = sp.invoice_payload
    from_name = user and html.quote(user.full_name) or "—"
    from_username = user and user.username and ("@" + user.username) or ""
    from_id = user and user.id or None
    chat_title = chat.title and html.quote(chat.title) or ""

    lines: list[str] = [
        html.bold("Donation received"),
        f"{html.bold('Amount:')} {stars} XTR",
        f"{html.bold('From:')} {from_name} {from_username} {('#u' + str(from_id)) if from_id else ''}",
        f"{html.bold('Origin:')} {chat.type} id={chat.id}{(' ' + chat_title) if chat_title else ''}",
        f"{html.bold('Target:')} {target_chat_id}{(' topic ' + str(target_thread_id)) if target_thread_id else ''}",
        f"{html.bold('Message ID:')} {message.message_id}",
        f"{html.bold('Payload:')} {html.code(payload)}",
    ]
    if sp.telegram_payment_charge_id:
        lines.append(
            f"{html.bold('Telegram charge:')} {html.code(sp.telegram_payment_charge_id)}"
        )
    if sp.provider_payment_charge_id:
        lines.append(
            f"{html.bold('Provider charge:')} {html.code(sp.provider_payment_charge_id)}"
        )

    for operator_id in sorted(settings.operator_ids):
        try:
            await bot.send_message(operator_id, "\n".join(lines))
        except Exception as exc:
            report_exception(
                "donation_operator_notify_failed",
                exception=exc,
                level="warning",
                operator_id=operator_id,
            )
    if settings.operator_ids:
        logfire.info(
            "donation_operators_notified",
            operator_count=len(settings.operator_ids),
            stars=stars,
            chat_id=chat.id,
            thread_id=message.message_thread_id,
            user_id=(user and user.id),
        )
