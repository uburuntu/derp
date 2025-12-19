"""UI helpers for credit system."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from derp.credits.packs import CREDIT_PACKS


def build_buy_keyboard(chat_id: int | None = None) -> InlineKeyboardMarkup:
    """Build inline keyboard with buy buttons.

    Args:
        chat_id: If provided, credits go to chat pool. Otherwise personal.
    """
    target = f"chat:{chat_id}" if chat_id else "user"

    buttons = []
    for pack in CREDIT_PACKS.values():
        if pack.bonus_pct > 0:
            label = f"⭐ {pack.stars} → {pack.credits} credits (+{pack.bonus_pct}%)"
        else:
            label = f"⭐ {pack.stars} → {pack.credits} credits"

        buttons.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"buy:{pack.id}:{target}",
                )
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=buttons)
