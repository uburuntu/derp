"""Fail-closed boundary for premium features awaiting durable migration."""

from __future__ import annotations

from typing import Final

from aiogram import Router
from aiogram.types import Message
from aiogram.utils.i18n import gettext as _

from derp.filters.meta import MetaCommand

SUSPENDED_PREMIUM_COMMANDS: Final = ("think", "video", "vid", "veo")

router = Router(name="premium_suspension")


@router.message(MetaCommand(*SUSPENDED_PREMIUM_COMMANDS))
async def handle_suspended_premium(message: Message) -> Message:
    """Intercept unsafe premium paths before the general chat handler."""
    return await message.reply(
        _("This premium feature is temporarily unavailable. Not charged.")
    )


__all__ = [
    "SUSPENDED_PREMIUM_COMMANDS",
    "handle_suspended_premium",
    "router",
]
