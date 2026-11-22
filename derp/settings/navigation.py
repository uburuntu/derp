"""Navigation and stable callback data for the Telegram settings card."""

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton

from derp.locales import t
from derp.telegram.buttons import button, dismiss_button


class SettingsAction(CallbackData, prefix="set"):
    action: str
    value: str = ""


def act(action: str, value: str = "") -> SettingsAction:
    return SettingsAction(action=action, value=value)


def page_button(locale: str, page: str, *, label: str | None = None) -> InlineKeyboardButton:
    return button(label or t(locale, f"navigation.{page}"), act("page", page))


def footer(locale: str, parent: str = "main") -> list[InlineKeyboardButton]:
    return [page_button(locale, parent, label=t(locale, "settings.back")), dismiss_button(locale)]
