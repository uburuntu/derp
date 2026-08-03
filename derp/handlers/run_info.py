"""Reply-based user receipts for delivered AI work."""

from __future__ import annotations

from html import escape

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.utils.i18n import gettext as _

from derp.config import settings
from derp.db import DatabaseManager
from derp.history.capture import suppress_outbound_history
from derp.run_info import RunInfo, RunInfoService, RunPrivacyMode

router = Router(name="run_info")


@router.message(Command("info"))
async def show_run_info(message: Message, db: DatabaseManager) -> None:
    """Describe the exact AI run behind a replied-to Derp message."""
    target = message.reply_to_message
    if (
        message.from_user is None
        or target is None
        or target.from_user is None
        or target.from_user.id != settings.bot_id
    ):
        with suppress_outbound_history():
            await message.reply(_("Reply to one of my answers with /info."))
        return

    info = await RunInfoService(db.read_session).get_for_telegram_message(
        telegram_chat_id=message.chat.id,
        telegram_message_id=target.message_id,
        viewer_telegram_id=message.from_user.id,
    )
    with suppress_outbound_history():
        if info is None:
            await message.reply(_("I don't have model details for that message."))
            return
        await message.reply(_render_run_info(info))


def _render_run_info(info: RunInfo) -> str:
    privacy = (
        _("Private model")
        if info.privacy_mode is RunPrivacyMode.PRIVATE
        else _("Free model")
    )
    lines = [
        _("<b>About this answer</b>"),
        _("<b>Model:</b> {model}").format(model=escape(info.model_display_name)),
        _("<b>Privacy:</b> {privacy}").format(privacy=privacy),
    ]

    if tokens := info.tokens:
        lines.append(
            _("<b>Tokens:</b> {input} in · {output} out").format(
                input=tokens.input_tokens,
                output=tokens.output_tokens,
            )
        )
        extras = []
        if tokens.cache_read_tokens:
            extras.append(_("{count} cached").format(count=tokens.cache_read_tokens))
        if tokens.reasoning_tokens:
            extras.append(_("{count} reasoning").format(count=tokens.reasoning_tokens))
        if extras:
            lines.append(" · ".join(extras))
    else:
        lines.append(_("<b>Tokens:</b> Not reported"))

    if info.context_estimated_tokens is not None:
        messages = _(
            "{count} message",
            "{count} messages",
            info.context_messages or 0,
        ).format(count=info.context_messages or 0)
        lines.append(
            _("<b>Context:</b> ~{tokens} tokens · {messages}").format(
                tokens=info.context_estimated_tokens,
                messages=messages,
            )
        )

    if not info.charge_visible:
        lines.append(_("<b>Charged:</b> Only the requester can see this"))
    elif info.charged_credits is None:
        lines.append(_("<b>Charged:</b> Still settling"))
    else:
        credit_count = _(
            "{count} credit",
            "{count} credits",
            info.charged_credits,
        ).format(count=info.charged_credits)
        lines.append(_("<b>Charged:</b> {credits}").format(credits=credit_count))
    return "\n".join(lines)


__all__ = ["router"]
