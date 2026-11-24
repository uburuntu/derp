from typing import TYPE_CHECKING, cast

from aiogram import Bot, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, Message

from derp.telegram.buttons import Press, button, link, markup, on_press, rich_button, rich_row

if TYPE_CHECKING:  # a type only the type checker sees, like the runtime in approvals
    from derp.agent.runtime import Runtime


class Pick(CallbackData, prefix="pick"):
    n: int


class Query:
    def __init__(self) -> None:
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


def test_markup_drops_empty_rows_and_wraps_bare_buttons() -> None:
    one, two = button("1", Pick(n=1)), button("2", Pick(n=2), style="success")
    keyboard = markup([one, None], None, two, [None], link("go", "https://t.me/DerpRobot?start=buy"))
    assert keyboard is not None
    assert [[b.text for b in row] for row in keyboard.inline_keyboard] == [["1"], ["2"], ["go"]]
    assert keyboard.inline_keyboard[2][0].url == "https://t.me/DerpRobot?start=buy"
    assert keyboard.inline_keyboard[0][0].callback_data == "pick:1"
    assert keyboard.inline_keyboard[1][0].style == "success"
    assert markup(None, [None]) is None


def test_rich_buttons_are_placed_in_markdown_with_escaped_labels() -> None:
    assert rich_button("<b>&", Pick(n=1), style="primary") == (
        '<tg-button type="callback_data" style="primary" data="pick:1">&lt;b&gt;&amp;</tg-button>'
    )
    assert rich_row(rich_button("1", Pick(n=1)), None, align="left") == (
        '<tg-button-row align="left"><tg-button type="callback_data" data="pick:1">1</tg-button></tg-button-row>'
    )
    assert rich_row(None) == ""


def test_a_press_counts_only_buttons_on_the_message_keyboard_or_rich_body() -> None:
    payload = {
        "message_id": 5,
        "date": 0,
        "chat": {"id": 1, "type": "private"},
        "rich_message": {
            "blocks": [
                {
                    "type": "paragraph",
                    "text": [
                        "Cap $200 ",
                        {"type": "button", "button": {"text": "adjust", "callback_data": "pick:2"}},
                    ],
                },
                {"type": "buttons", "buttons": [{"text": "OK", "callback_data": "pick:3"}]},
            ]
        },
    }
    message = Message.model_validate(payload)

    def pressed(data: str) -> bool:
        query = CallbackQuery.model_validate(
            {
                "id": "q",
                "from": {"id": 1, "is_bot": False, "first_name": "A"},
                "chat_instance": "c",
                "data": data,
            }
        )
        return Press(query, message, cast(Bot, None), "en").has_pressed_button()

    assert pressed("pick:2") and pressed("pick:3") and not pressed("pick:9")


async def test_a_press_is_answered_once() -> None:
    query = Query()
    press = Press(cast(CallbackQuery, query), cast(Message, None), cast(Bot, None), "en")
    await press.deny("buttons.stale")
    await press.toast()
    assert query.answers == [("These buttons are out of date.", True)]
    assert press.answered


async def test_owner_copy_is_answered_as_written() -> None:
    query = Query()
    press = Press(cast(CallbackQuery, query), cast(Message, None), cast(Bot, None), "ru")
    await press.answer("Proposal #1 applied.")
    await press.deny("buttons.stale")
    assert query.answers == [("Proposal #1 applied.", False)]


def test_handlers_may_name_types_imported_only_for_type_checking() -> None:
    async def handler(press: Press, data: Pick, runtime: Runtime) -> None: ...

    router = Router()
    assert on_press(router, Pick)(handler) is handler
    assert len(router.callback_query.handlers) == 1
