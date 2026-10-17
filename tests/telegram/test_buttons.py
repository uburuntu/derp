from typing import cast

from aiogram import Bot
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, Message

from derp.telegram.buttons import Press, button, markup


class Pick(CallbackData, prefix="pick"):
    n: int


class Query:
    def __init__(self) -> None:
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


def test_markup_drops_empty_rows_and_wraps_bare_buttons() -> None:
    one, two = button("1", Pick(n=1)), button("2", Pick(n=2), style="success")
    keyboard = markup([one, None], None, two, [None])
    assert keyboard is not None
    assert [[b.text for b in row] for row in keyboard.inline_keyboard] == [["1"], ["2"]]
    assert keyboard.inline_keyboard[0][0].callback_data == "pick:1"
    assert keyboard.inline_keyboard[1][0].style == "success"
    assert markup(None, [None]) is None


async def test_a_press_is_answered_once() -> None:
    query = Query()
    press = Press(cast(CallbackQuery, query), cast(Message, None), cast(Bot, None), "en")
    await press.deny("buttons.stale")
    await press.toast()
    assert query.answers == [("These buttons are out of date.", True)]
    assert press.answered
