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
    credit_service: CreditService,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
) -> Message:
    text = meta.target_text
    if not text:
        return await message.reply(_("Usage: /tts <text>"))

    if not user_model or not chat_model:
        return await message.reply(
            _("😅 Could not verify your access. Please try again.")
        )

    access = await credit_service.check_tool_access(
        user_model, chat_model, "voice_tts", TTS_MODEL
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
            db=get_db_manager(),
            bot=message.bot,
            user_model=user_model,
            chat_model=chat_model,
        )
        await generate_and_send_tts(deps, text=text, model=TTS_MODEL)

        idempotency_key = f"voice_tts:{chat_model.telegram_id}:{message.message_id}"
        await credit_service.deduct(
            access,
            user_model,
            chat_model,
            "voice_tts",
            idempotency_key=idempotency_key,
            metadata={"model": TTS_MODEL},
        )

        logfire.info(
            "tts_command_ok",
            user_id=user_model.telegram_id,
            chat_id=chat_model.telegram_id,
        )
        return message
    except Exception:
        logfire.exception("tts_command_failed")
        return await message.reply(
            _(
                "😅 Something went wrong while generating the voice message. Try again later."
            )
        )
