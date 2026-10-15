from aiogram.types import Message

from derp.telegram.sender import Target, sanitize_untrusted, split_rich
from derp.telegram.text import split_text, truncate, utf16_len


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
