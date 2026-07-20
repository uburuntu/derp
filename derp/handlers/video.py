"""Video generation handler (/video) using Veo 3.1.

Uses google-genai SDK under the hood (see derp/tools/veo_video.py) and shares
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

from derp.common.sender import MessageSender
from derp.credits import CreditService
from derp.credits.purchase_suspension import purchase_suspension_message
from derp.db import get_db_manager
from derp.filters.meta import MetaCommand, MetaInfo
from derp.models import Chat as ChatModel
from derp.models import User as UserModel
from derp.observability import report_exception
from derp.tools.veo_video import generate_and_send_video

router = Router(name="video")


def _parse_video_request(meta: MetaInfo) -> tuple[str, str]:
    """Resolve quality and remove an explicit command quality from the prompt."""
    quality = "fast"
    has_explicit_quality = False
    if meta.arguments:
        candidate = meta.arguments[0].strip().lower()
        if candidate in {"fast", "standard"}:
            quality = candidate
            has_explicit_quality = True

    prompt = meta.target_text
    first, separator, remainder = prompt.partition(" ")
    if has_explicit_quality and meta.command and first.lower() == quality:
        prompt = remainder.strip() if separator else ""
    return quality, prompt


@router.message(MetaCommand("video", "vid", "veo"))
@flags.chat_action(initial_sleep=2, action="upload_video")
async def handle_video(
    message: Message,
    sender: MessageSender,
    meta: MetaInfo,
    credit_service: CreditService,
    user_model: UserModel | None = None,
    chat_model: ChatModel | None = None,
) -> Message:
    quality, prompt = _parse_video_request(meta)
    if not prompt:
        return await message.reply(
            _("Usage: /video [fast|standard] <prompt>"),
        )

    if not user_model or not chat_model:
        return await message.reply(
            _("😅 Could not verify your access. Please try again.")
        )

    duration_seconds = 6
    access = await credit_service.check_tool_access(
        user_model,
        chat_model,
        "video_generate",
        arguments={"quality": quality, "duration_seconds": duration_seconds},
    )
    if not access.allowed:
        return await sender.reply(
            _("🎬 Video generation requires credits.\n\n✨ {reason}").format(
                reason=access.reject_reason or ""
            )
            + "\n\n"
            + purchase_suspension_message(),
        )
    plan = access.require_plan()
    model = plan.model

    try:
        from derp.llm.deps import AgentDeps

        deps_obj = AgentDeps(
            message=message,
            db=get_db_manager(),
            bot=message.bot,
            user_model=user_model,
            chat_model=chat_model,
            model=model,
        )

        await generate_and_send_video(
            deps_obj,
            prompt=prompt,
            duration_seconds=duration_seconds,
            plan=plan,
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
            metadata={
                "quality": quality,
                "duration_seconds": duration_seconds,
                "model": model.provider_model_id,
            },
        )

        logfire.info(
            "video_command_ok",
            user_id=user_model.telegram_id,
            chat_id=chat_model.telegram_id,
            quality=quality,
            model=model.provider_model_id,
        )
        return message
    except Exception:
        report_exception("video_command_failed")
        return await message.reply(
            _("😅 Something went wrong while generating the video. Try again later.")
        )
