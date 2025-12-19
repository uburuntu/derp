"""Text-to-speech handler (/tts).

Shares credit checks and limits with the agent tool `voice_tts`.

Models: https://ai.google.dev/gemini-api/docs/models.md.txt
Pricing: https://ai.google.dev/gemini-api/docs/pricing.md.txt
"""

from __future__ import annotations

import logfire
from aiogram import Router, flags
from aiogram.types import Message
from aiogram.utils.i18n import gettext as _

from derp.credits import CreditService
from derp.db import get_db_manager
from derp.filters.meta import MetaCommand, MetaInfo
from derp.llm.deps import AgentDeps
from derp.models import Chat as ChatModel
from derp.models import User as UserModel
from derp.tools.tts import TTS_MODEL, generate_and_send_tts

router = Router(name="tts")


@router.message(MetaCommand("tts", "voice", "say"))
@flags.chat_action(initial_sleep=1, action="record_voice")
async def handle_tts(
    message: Message,
    meta: MetaInfo,
    chat_settings: ChatModel | None = None,
    user: UserModel | None = None,
) -> Message:
    text = meta.target_text
    if not text:
        return await message.reply(_("Usage: /tts <text>"))

    if not user or not chat_settings:
        return await message.reply(
            _("😅 Could not verify your access. Please try again.")
        )

    db = get_db_manager()
    async with db.session() as session:
        service = CreditService(session)
        access = await service.check_tool_access(
            user_id=user.id,
            chat_id=chat_settings.id,
            user_telegram_id=user.telegram_id,
            chat_telegram_id=chat_settings.telegram_id,
            tool_name="voice_tts",
            model_id=TTS_MODEL,
        )
        if not access.allowed:
            return await message.reply(
                _(
                    "🔊 Voice generation requires credits.\n\n✨ {reason}\n\n💡 Use /buy to get credits!"
                ).format(reason=access.reject_reason or ""),
                parse_mode="Markdown",
            )

        try:
            deps = AgentDeps(
                message=message,
                db=db,
                bot=message.bot,
                chat=chat_settings,
                user=user,
            )
            await generate_and_send_tts(deps, text=text, model=TTS_MODEL)

            idempotency_key = (
                f"voice_tts:{chat_settings.telegram_id}:{message.message_id}"
            )
            await service.deduct(
                access,
                user.id,
                chat_settings.id,
                "voice_tts",
                idempotency_key=idempotency_key,
                metadata={"model": TTS_MODEL},
            )

            logfire.info(
                "tts_command_ok",
                user_id=user.telegram_id,
                chat_id=chat_settings.telegram_id,
            )
            return message
        except Exception:
            logfire.exception("tts_command_failed")
            return await message.reply(
                _(
                    "😅 Something went wrong while generating the voice message. Try again later."
                )
            )
