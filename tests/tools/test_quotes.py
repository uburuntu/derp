import asyncio
import io
import random
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from derp.config import MediaLimits
from derp.tools import effects, quotes
from derp.tools.quotes import Quoted, Reply, Typesetter
from derp.tools.workers import EffectError, WorkerPool

BOLD, ITALIC, MONO, LINK, SPOILER = (frozenset({f}) for f in ("bold", "italic", "mono", "link", "spoiler"))
PLAIN: frozenset[str] = frozenset()
installed = pytest.mark.skipif(not quotes.EMOJI_FONT.exists(), reason="Noto Color Emoji not installed")


@pytest.fixture
def no_emoji_font(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(quotes, "EMOJI_FONT", Path("/nonexistent/emoji.ttf"))
    quotes._emoji_font.cache_clear()
    quotes._emoji_glyph.cache_clear()
    yield
    quotes._emoji_font.cache_clear()
    quotes._emoji_glyph.cache_clear()


def picture(color: str | tuple[int, int, int] = "teal", size: tuple[int, int] = (300, 200)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG")
    return buffer.getvalue()


def noise(side: int = 600) -> bytes:
    dice = random.Random(7)
    buffer = io.BytesIO()
    Image.frombytes("RGB", (side, side), dice.randbytes(side * side * 3)).save(buffer, format="PNG")
    return buffer.getvalue()


def opened(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def rendered(messages: list[Quoted], **options: Any) -> bytes:
    return quotes.render(messages, **options).data


def flagged(text: str, entities: list[dict[str, object]]) -> dict[str, frozenset[str]]:
    """Which characters carry which flags, joined into runs for easy reading."""
    runs: dict[str, frozenset[str]] = {}
    current, flags = "", PLAIN
    for char, mark in zip(text, quotes.styles(text, entities), strict=True):
        if mark != flags and current:
            runs[current] = flags
            current = ""
        current, flags = current + char, mark
    runs[current] = flags
    return {run: mark for run, mark in runs.items() if mark}


# Entities: offsets and lengths count UTF-16 code units.


def test_entities_after_an_emoji_count_its_two_utf16_units() -> None:
    assert flagged("😂 bold", [{"type": "bold", "offset": 3, "length": 4}]) == {"bold": BOLD}


def test_entities_over_cyrillic_and_skin_toned_emoji() -> None:
    text = "👍🏽 жирный и курсив"
    entities: list[dict[str, object]] = [
        {"type": "bold", "offset": 5, "length": 6},
        {"type": "italic", "offset": 14, "length": 6},
    ]
    assert flagged(text, entities) == {"жирный": BOLD, "курсив": ITALIC}


def test_entities_spanning_an_emoji_cover_the_whole_emoji() -> None:
    assert flagged("a 🎉 b", [{"type": "spoiler", "offset": 2, "length": 2}]) == {"🎉": SPOILER}


def test_links_code_and_nested_styles() -> None:
    text = "see x.com or `run` now"
    entities: list[dict[str, object]] = [
        {"type": "url", "offset": 4, "length": 5},
        {"type": "code", "offset": 13, "length": 5},
        {"type": "bold", "offset": 0, "length": 9},
    ]
    assert flagged(text, entities) == {"see ": BOLD, "x.com": frozenset({"bold", "link"}), "`run`": MONO}


def test_malformed_entities_are_skipped() -> None:
    entities: list[object] = [
        {"type": "bold"},
        {"type": "italic", "offset": "x", "length": 2},
        "garbage",
        {"type": "custom_emoji", "offset": 0, "length": 2},
        {"type": "bold", "offset": -3, "length": 2},
        {"type": "underline", "offset": 2, "length": 99},
    ]
    assert flagged("hello", entities) == {"llo": frozenset({"underline"})}  # pyright: ignore[reportArgumentType]
    assert quotes.styles("hi", {"not": "a list"}) == [PLAIN, PLAIN]


def test_text_is_cleaned_and_capped() -> None:
    text, marks = quotes.clean("a\tb\x00c\r\n" + "x" * 50, [{"type": "bold", "offset": 0, "length": 1}], 10)
    assert text == "a bc\nxxxx…" and marks[0] == BOLD and len(marks) == len(text)


def test_emoji_clusters() -> None:
    found = [m.group() for m in quotes.EMOJI.finditer("hi 😂👍🏽 ❤️ © ©️ 1️⃣ 🇷🇺 👨‍👩‍👧 x")]
    assert found == ["😂", "👍🏽", "❤️", "©️", "1️⃣", "🇷🇺", "👨‍👩‍👧"]


def test_without_an_emoji_font_the_text_font_draws_emoji(no_emoji_font: None) -> None:
    assert quotes.spans("ok 😂") == [quotes.Span("ok 😂")]
    assert opened(rendered([Quoted(1, "A", "ok 😂🇷🇺")])).format == "WEBP"


@installed
def test_emoji_get_spans_of_their_own() -> None:
    found = quotes.spans("ok 😂!", [BOLD] * 6)
    assert [(s.text, s.emoji) for s in found] == [("ok ", False), ("😂", True), ("!", False)]
    assert all(s.flags == BOLD for s in found)


# Layout


def test_wrapping_breaks_at_spaces_keeps_line_breaks_and_splits_long_words() -> None:
    setter = Typesetter(1, quotes.Theme.of(None))
    lines = setter.wrap(quotes.spans("one two\nthree " + "w" * 80), quotes.TEXT_SIZE, 120)
    texts = ["".join(p.span.text for p in line) for line in lines]
    assert texts[:2] == ["one two", "three"] and len(texts) > 3
    assert all(quotes._right(line) <= 120 for line in lines)
    assert "".join(texts[2:]) == "w" * 80


def test_consecutive_messages_of_one_sender_group_together() -> None:
    setter = Typesetter(1, quotes.Theme.of(None))
    messages = [Quoted(1, "Ann", "a"), Quoted(1, "Ann", "b"), Quoted(2, "Bob", "c"), Quoted(1, "Ann", "d")]
    bubbles, _ = setter.layout(messages, [None] * 4)
    assert [(b.first, b.last) for b in bubbles] == [(True, False), (False, True), (True, True), (True, True)]
    assert [bool(b.name) for b in bubbles] == [True, False, True, True]
    assert bubbles[1].y - (bubbles[0].y + bubbles[0].height) < bubbles[2].y - (
        bubbles[1].y + bubbles[1].height
    )


def test_a_reply_header_and_media_make_room() -> None:
    setter = Typesetter(1, quotes.Theme.of(None))
    plain = setter.bubble(Quoted(1, "Ann", "hi"), None, first=True, last=True)
    replying = setter.bubble(
        Quoted(1, "Ann", "hi", reply=Reply("Bob", 2, "a very long line " * 20)), None, first=True, last=True
    )
    assert replying.reply is not None and replying.height > plain.height
    name, text = replying.reply
    assert text[-1].span.text == "…" and "".join(p.span.text for p in name) == "Bob"
    image = Image.new("RGB", (900, 300))
    photo = setter.bubble(Quoted(1, "Ann", "hi"), image, first=True, last=True)
    assert photo.media == (300, 100)
    sticker = setter.bubble(Quoted(1, "Ann", sticker=True), image, first=False, last=True)
    assert sticker.bare and sticker.media is not None and max(sticker.media) == quotes.STICKER_BOX


def test_initials() -> None:
    assert quotes.initials("Liv Tyler") == "LT"
    assert quotes.initials("борис") == "Б"
    assert quotes.initials("😂 Joker Smith Jr") == "JS"
    assert quotes.initials("") == "?"


# Output


def test_a_sticker_is_transparent_webp_512_px_under_512_kb() -> None:
    data = rendered([Quoted(1, "Ann", "short")])
    image = opened(data)
    assert image.format == "WEBP" and image.mode == "RGBA"
    assert max(image.size) == 512 and min(image.size) <= 512 and len(data) <= 512 * 1024
    assert image.getpixel((0, 0))[3] == 0  # pyright: ignore[reportOptionalSubscript, reportIndexIssue]


def test_a_busy_quote_stays_within_the_sticker_limits() -> None:
    long = "Съешь же ещё этих мягких французских булок, да выпей чаю. " * 30
    messages = [
        Quoted(i % 3, f"Person {i}", long, avatar=noise(), media=noise() if i % 2 else None)
        for i in range(14)
    ]
    quote = quotes.render(messages)
    assert quote.output == "image"  # far too long to read at sticker size
    assert opened(quote.data).format == "PNG"


def test_a_sticker_is_only_made_while_its_text_stays_readable() -> None:
    line = "Съешь же ещё этих мягких французских булок, да выпей чаю."
    short = quotes.render([Quoted(1, "Ann", line), Quoted(2, "Bob", "ага")])
    assert short.output == "sticker" and opened(short.data).format == "WEBP"
    long = quotes.render([Quoted(i % 2, f"P{i}", line * 3) for i in range(6)])
    assert long.output == "image"
    picture = opened(long.data)
    assert picture.format == "PNG" and picture.mode == "RGB"  # a photo: flat backdrop, no transparency


def test_too_many_messages_are_cut_to_the_limit() -> None:
    messages = [Quoted(i, f"P{i}", "x") for i in range(quotes.MAX_MESSAGES + 5)]
    assert len(quotes._within_budget(messages)) == quotes.MAX_MESSAGES
    cut = quotes._within_budget([Quoted(1, "A", "y" * 2000)] * 3)
    assert [len(m.text) for m in cut] == [2000, 1000, 1] and cut[1].text.endswith("…") and cut[2].text == "…"


def test_an_image_is_a_png_on_a_backdrop_matching_the_bubble() -> None:
    quote = quotes.render([Quoted(1, "Ann", "hi")], output="image", bubble="pink")
    image = opened(quote.data)
    assert quote.output == "image" and image.format == "PNG" and image.mode == "RGB"
    assert image.getpixel((0, 0)) == quotes.Theme.of("pink").backdrop


def _reddish(data: bytes) -> int:
    image = opened(data).convert("RGBA")
    return sum(1 for r, g, b, a in image.get_flattened_data() if a > 200 and r > 220 and g < 40 and b < 40)  # pyright: ignore[reportGeneralTypeIssues]


def test_the_avatar_is_drawn_and_garbage_falls_back_to_initials() -> None:
    with_photo = rendered([Quoted(1, "Ann", "hi", avatar=picture((255, 0, 0)))], output="image")
    assert _reddish(with_photo) > 100
    for broken in (b"not an image", b"", None, b"\x89PNG\r\n\x1a\n garbage"):
        assert _reddish(rendered([Quoted(1, "Ann", "hi", avatar=broken)], output="image")) == 0


def test_garbage_media_is_left_out() -> None:
    data = rendered([Quoted(1, "Ann", "hi", media=b"\xff\xd8\xff garbage")])
    assert opened(data).format == "WEBP"
    assert quotes.render([Quoted(1, "Ann", "", media=b"junk", sticker=True)])  # no picture, no text: a bubble


def test_every_style_renders() -> None:
    text = "bold italic code link under strike secret"
    kinds = ["bold", "italic", "code", "url", "underline", "strikethrough", "spoiler"]
    entities = [{"type": kind, "offset": text.find(word), "length": len(word)} for kind, word in zip(
        kinds, text.split(), strict=True
    )]  # fmt: skip
    reply = Reply("Bob", 2, "earlier")
    assert opened(rendered([Quoted(1, "Ann", text, entities, reply=reply)])).format == "WEBP"
    assert opened(rendered([Quoted(1, "Ann", text, entities)], bubble="#ffffff")).format == "WEBP"


def test_an_unknown_colour_is_refused() -> None:
    with pytest.raises(EffectError, match="unknown colour"):
        quotes.render([Quoted(1, "Ann", "hi")], bubble="definitely not a colour")


def test_the_spoiler_cloud_is_the_same_every_time() -> None:
    entities = [{"type": "spoiler", "offset": 0, "length": 6}]
    assert quotes.render([Quoted(1, "A", "secret", entities)]) == quotes.render(
        [Quoted(1, "A", "secret", entities)]
    )


def test_the_pool_leaves_out_pictures_over_the_input_cap() -> None:
    pool = WorkerPool(MediaLimits(max_input_bytes=1000))
    big = noise(100)
    quote = asyncio.run(effects.quote(pool, [Quoted(1, "Ann", "hi", avatar=big, media=big)], "sticker"))
    assert opened(quote.data).format == "WEBP"
