"""Video generation handler (/video) using Veo 3.1.

Uses google-genai SDK under the hood (see derp/tools/video_gen.py) and shares
credit limits/pricing with the agent tool `video_generate`.

Docs:
- Models: https://ai.google.dev/gemini-api/docs/models.md.txt
- Video: https://ai.google.dev/gemini-api/docs/video.md.txt
"""

from __future__ import annotations

import logfire
from aiogram import Router, flags
from aiogram.types import Message
from aiogram.utils.i18n import gettext as _

from derp.credits import CreditService
from derp.db import get_db_manager
from derp.filters.meta import MetaCommand, MetaInfo
from derp.models import Chat as ChatModel
from derp.models import User as UserModel
from derp.tools.video_gen import VEO_31_FAST, VEO_31_STANDARD, generate_and_send_video

router = Router(name="video")


def _parse_quality(meta: MetaInfo) -> str:
    # Accept: /video fast ... or /video standard ...
    if not meta.arguments:
        return "fast"
    q = meta.arguments[0].strip().lower()
    return q if q in ("fast", "standard") else "fast"


@router.message(MetaCommand("video", "vid", "veo"))
@flags.chat_action(initial_sleep=2, action="upload_video")
async def handle_video(
    message: Message,
    meta: MetaInfo,
    credit_service: CreditService,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
) -> Message:
    prompt = meta.target_text
    if not prompt:
        return await message.reply(
            _("Usage: /video [fast|standard] <prompt>"),
        )

    if not user_model or not chat_model:
        return await message.reply(
            _("😅 Could not verify your access. Please try again.")
        )

    quality = _parse_quality(meta)
    model_id = VEO_31_STANDARD if quality == "standard" else VEO_31_FAST

    access = await credit_service.check_tool_access(
        user_model, chat_model, "video_generate", model_id
    )
    if not access.allowed:
        return await message.reply(
            _(
                "🎬 Video generation requires credits.\n\n✨ {reason}\n\n💡 Use /buy to get credits!"
            ).format(reason=access.reject_reason or ""),
            parse_mode="Markdown",
        )

    try:
        from derp.llm.deps import AgentDeps

        deps_obj = AgentDeps(
            message=message,
            db=get_db_manager(),
            bot=message.bot,
            user_model=user_model,
            chat_model=chat_model,
        )

        await generate_and_send_video(
            deps_obj,
            prompt=prompt,
            quality=quality,
            model=model_id,
        )

        idempotency_key = (
            f"video_generate:{chat_model.telegram_id}:{message.message_id}"
        )
        await credit_service.deduct(
            access,
            user_model,
            chat_model,
            "video_generate",
            idempotency_key=idempotency_key,
            metadata={"quality": quality, "model": model_id},
        )

        logfire.info(
            "video_command_ok",
            user_id=user_model.telegram_id,
            chat_id=chat_model.telegram_id,
            quality=quality,
            model=model_id,
        )
        return message
    except Exception:
        logfire.exception("video_command_failed")
        return await message.reply(
            _("😅 Something went wrong while generating the video. Try again later.")
        )
