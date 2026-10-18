"""Telegram-looking quotes drawn with Pillow: bubbles, coloured names, avatars, reply bars, rich text, emoji.

Input is plain data (``Quoted``): the agent tool resolves stored messages and fetches avatars and media
first, so nothing here touches the network, the database or a file id. Text keeps its Telegram entities,
whose offsets count UTF-16 code units. Consecutive messages of one sender form a run: the name on its
first bubble, the avatar (or an initials circle) beside its last. A quote is laid out once at 1× to learn
its size, then drawn at twice the output density and scaled down, so curves and text come out smooth.
Colour emoji come from Noto Color Emoji when it is installed (the image has ``fonts-noto-color-emoji``),
else the text font draws them. Bytes that won't open as a picture give an initials circle or no media,
never an error.
"""

import io
import math
import random
import re
from bisect import bisect_left
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import cache, lru_cache
from pathlib import Path
from typing import Any, Literal

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFont, features

from derp.tools.captions import FONTS, encode_sticker, open_image
from derp.tools.workers import DEFAULTS, EffectError, check_dimensions

Output = Literal["sticker", "image"]
RGB = tuple[int, int, int]
Flags = frozenset[str]

REGULAR = FONTS / "NotoSans-Regular.ttf"
BOLD = FONTS / "NotoSans-Bold.ttf"
ITALIC = FONTS / "NotoSans-Italic.ttf"
MONO = FONTS / "NotoSansMono-Regular.ttf"
EMOJI_FONT = Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf")  # Debian fonts-noto-color-emoji
EMOJI_STRIKE = 109  # the one bitmap size Noto Color Emoji has

MAX_MESSAGES = 10
MAX_CHARS = 1000  # of one message
MAX_TOTAL_CHARS = 3000
PNG_SIDE = 2048
STICKER_SIDE = 512

# Geometry at 1×, in pixels.
TEXT_SIZE, NAME_SIZE, REPLY_SIZE = 16, 15, 14
LINE = 1.32  # line height per font size
PAD_X, PAD_Y = 12, 8
RADIUS, TIGHT = 16, 6
AVATAR, GAP = 40, 8
RUN_GAP, STACK_GAP = 10, 4
TEXT_WIDTH = 300
MEDIA_BOX = (300, 320)
STICKER_BOX = 160
MARGIN = 4

DEFAULT_BUBBLE = "#1f2c3b"
# Telegram's seven peer colours (red, orange, violet, green, cyan, blue, pink), picked by id.
NAMES_DARK = ("#ff8e86", "#ffa357", "#b18fff", "#7dd663", "#45d8e8", "#6ab8ff", "#ff7fd5")
NAMES_LIGHT = ("#e5463b", "#e07000", "#8051d3", "#2e9e3b", "#0b9fb0", "#2a8bd8", "#c9449f")
AVATARS = ("#ff5e5e", "#ff9a3d", "#9e7bff", "#5ccb5f", "#3cc4d5", "#4fa6f5", "#f569c4")

# Entity types and how they draw; the rest (custom emoji, blockquotes, dates) draw as plain text.
FLAGS = {
    "bold": "bold", "italic": "italic", "code": "mono", "pre": "mono", "underline": "underline",
    "strikethrough": "strike", "spoiler": "spoiler", "url": "link", "text_link": "link", "mention": "link",
    "text_mention": "link", "hashtag": "link", "cashtag": "link", "bot_command": "link", "email": "link",
    "phone_number": "link",
}  # fmt: skip
PICTOGRAPH = "\U0001f000-\U0001faff\u2300-\u23ff\u2600-\u27bf\u2b00-\u2bff"
# Symbols that are emoji only when followed by the emoji presentation selector (VS16).
TEXT_SYMBOLS = "\u00a9\u00ae\u203c\u2049\u2122\u2139\u2194-\u21ff\u24c2\u25aa-\u25fe\u3030\u303d\u3297\u3299"
MODIFIERS = "\U0001f3fb-\U0001f3ff"  # skin tones
VS15, VS16, ZWJ, KEYCAP = "\ufe0e", "\ufe0f", "\u200d", "\u20e3"
EMOJI = re.compile(
    "[\U0001f1e6-\U0001f1ff]{2}"  # flags
    f"|[0-9#*]{VS16}?{KEYCAP}"
    f"|(?:[{PICTOGRAPH}]|[{TEXT_SYMBOLS}]{VS16})[{VS15}{VS16}]?[{MODIFIERS}]?"
    f"(?:{ZWJ}[{PICTOGRAPH}]{VS16}?[{MODIFIERS}]?)*[\U000e0020-\U000e007f]*"  # joined sequences, tags
)
SIMPLE = str.maketrans("", "", VS15 + VS16 + "".join(map(chr, range(0x1F3FB, 0x1F400))))


@dataclass(frozen=True, slots=True)
class Reply:
    """The bar above a bubble: who was replied to and a line of what they said."""

    name: str
    peer: int
    text: str


@dataclass(frozen=True, slots=True)
class Quoted:
    """One message as the quote shows it."""

    peer: int  # the sender's id: name colour, and consecutive messages of one peer group together
    name: str
    text: str = ""
    entities: Sequence[Mapping[str, Any]] = ()  # Telegram entities, offsets in UTF-16 code units
    avatar: bytes | None = None
    media: bytes | None = None  # a photo, or a sticker when ``sticker``
    sticker: bool = False
    reply: Reply | None = None


@dataclass(frozen=True, slots=True)
class Span:
    """Text in one style; an emoji (with its modifiers and joiners) is a span of its own."""

    text: str
    flags: Flags = frozenset()
    emoji: bool = False


@dataclass(slots=True)
class Piece:
    span: Span
    x: float
    width: float


Line = list[Piece]


@dataclass(slots=True)
class Bubble:
    message: Quoted
    first: bool  # of its sender's run: shows the name
    last: bool  # of its sender's run: the avatar sits beside it
    width: int
    height: int
    bare: bool = False  # a lone sticker: drawn without a bubble
    name: Line = field(default_factory=Line)
    reply: tuple[Line, Line] | None = None
    media: tuple[int, int] | None = None  # drawn size
    lines: list[Line] = field(default_factory=list[Line])
    content: int = 0  # inner width
    x: int = 0
    y: int = 0


@dataclass(frozen=True, slots=True)
class Theme:
    bubble: RGB
    text: RGB
    link: RGB
    code: RGB
    names: tuple[str, ...]

    @classmethod
    def of(cls, colour: str | None) -> Theme:
        try:
            rgb = ImageColor.getrgb((colour or DEFAULT_BUBBLE).strip())
        except ValueError:
            raise EffectError("unknown colour (use a name like pink or a hex code like #1b1429)") from None
        bubble = (rgb[0], rgb[1], rgb[2])
        if 0.2126 * bubble[0] + 0.7152 * bubble[1] + 0.0722 * bubble[2] < 150:
            return cls(bubble, (255, 255, 255), (113, 186, 250), (126, 224, 166), NAMES_DARK)
        return cls(bubble, (0, 0, 0), (36, 129, 204), (31, 138, 76), NAMES_LIGHT)

    def name(self, peer: int) -> RGB:
        return _rgb(self.names[abs(peer) % len(self.names)])


def _rgb(colour: str) -> RGB:
    red, green, blue = ImageColor.getrgb(colour)[:3]
    return red, green, blue


def mix(colour: RGB, base: RGB, amount: float) -> RGB:
    """``colour`` laid over ``base`` at ``amount`` opacity, as an opaque colour."""
    red, green, blue = (round(c * amount + b * (1 - amount)) for c, b in zip(colour, base, strict=True))
    return red, green, blue


@cache
def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


@cache
def _emoji_font() -> ImageFont.FreeTypeFont | None:
    try:
        return ImageFont.truetype(str(EMOJI_FONT), EMOJI_STRIKE)
    except OSError:  # not installed (dev machines): the text font draws emoji
        return None


@lru_cache(maxsize=512)
def _emoji_glyph(cluster: str) -> Image.Image | None:
    """The emoji at the font's native size, or None when the font has nothing for it."""
    font = _emoji_font()
    if font is None:
        return None
    if not features.check_feature("raqm"):  # without shaping, joined sequences fall apart: draw the base
        cluster = cluster.split(ZWJ)[0].translate(SIMPLE) or cluster
    ascent, descent = font.getmetrics()
    width = math.ceil(font.getlength(cluster))
    if width <= 0:
        return None
    glyph = Image.new("RGBA", (width, ascent + descent))
    ImageDraw.Draw(glyph).text((0, 0), cluster, font=font, embedded_color=True)
    return glyph if glyph.getbbox() else None


@lru_cache(maxsize=512)
def _emoji_image(cluster: str, width: int, height: int) -> Image.Image | None:
    glyph = _emoji_glyph(cluster)
    return glyph.resize((width, height), Image.Resampling.LANCZOS) if glyph else None


def styles(text: str, entities: object) -> list[Flags]:
    """Each character's flags from Telegram entities (UTF-16 offsets); malformed entities are skipped."""
    starts: list[int] = []
    position = 0
    for char in text:
        starts.append(position)
        position += 2 if ord(char) > 0xFFFF else 1
    marks: list[set[str]] = [set() for _ in text]
    for entity in entities if isinstance(entities, list | tuple) else ():
        try:
            flag = FLAGS.get(str(entity["type"]))
            offset, length = int(entity["offset"]), int(entity["length"])
        except KeyError, TypeError, ValueError:
            continue
        if flag is None or length <= 0 or offset < 0:
            continue
        for index in range(bisect_left(starts, offset), bisect_left(starts, offset + length)):
            marks[index].add(flag)
    return [frozenset(mark) for mark in marks]


def spans(text: str, flags: Sequence[Flags] | None = None) -> list[Span]:
    """Styled text cut into spans: runs of one style, and each emoji on its own (when there's an emoji font)."""
    marks = list(flags) if flags is not None else [frozenset[str]()] * len(text)
    found: list[Span] = []

    def plain(start: int, end: int) -> None:
        while start < end:
            stop = start
            while stop < end and marks[stop] == marks[start]:
                stop += 1
            found.append(Span(text[start:stop], marks[start]))
            start = stop

    done = 0
    if _emoji_font() is not None:
        for match in EMOJI.finditer(text):
            plain(done, match.start())
            found.append(Span(match.group(), marks[match.start()], emoji=True))
            done = match.end()
    plain(done, len(text))
    return found


def clean(text: str, entities: object, limit: int) -> tuple[str, list[Flags]]:
    """Text and flags with control characters dropped, tabs as spaces and the length capped with "…"."""
    flags = styles(text, entities)
    chars: list[str] = []
    marks: list[Flags] = []
    for char, mark in zip(text, flags, strict=True):
        if char == "\t":
            char = " "
        elif char != "\n" and (ord(char) < 32 or char == "\r"):
            continue
        chars.append(char)
        marks.append(mark)
    if len(chars) > limit:
        chars, marks = [*chars[: limit - 1], "…"], [*marks[: limit - 1], frozenset()]
    while chars and chars[-1].isspace():
        chars.pop()
        marks.pop()
    return "".join(chars), marks


def _words(found: Sequence[Span]) -> Iterator[list[Span] | Span | None]:
    """Words (lists of spans with no space between them), single-space spans and line breaks (None)."""
    word: list[Span] = []
    for span in found:
        if span.emoji:
            word.append(span)
            continue
        for part in re.split(r"(\n| +)", span.text):
            if not part:
                continue
            if part == "\n" or part.isspace():
                if word:
                    yield word
                    word = []
                yield None if part == "\n" else Span(" ", span.flags)
            else:
                word.append(Span(part, span.flags))
    if word:
        yield word


class Typesetter:
    """Measures and draws at one scale (1× for the size, then the drawing density)."""

    def __init__(self, scale: float, theme: Theme) -> None:
        self.scale = scale
        self.theme = theme

    def px(self, value: float) -> int:
        return max(1, round(value * self.scale))

    def font(self, flags: Flags, size: float) -> ImageFont.FreeTypeFont:
        path = (
            MONO if "mono" in flags else BOLD if "bold" in flags else ITALIC if "italic" in flags else REGULAR
        )
        return _font(path, self.px(size))

    def emoji_box(self, cluster: str, size: float) -> tuple[int, int] | None:
        glyph = _emoji_glyph(cluster)
        if glyph is None:
            return None
        height = self.px(size * 1.2)
        return max(1, round(height * glyph.width / glyph.height)), height

    def width(self, span: Span, size: float) -> float:
        if span.emoji and (box := self.emoji_box(span.text, size)):
            return box[0] + self.px(1)
        return self.font(span.flags, size).getlength(span.text)

    def line_height(self, size: float) -> int:
        return self.px(size * LINE)

    def wrap(self, found: Sequence[Span], size: float, width: float) -> list[Line]:
        """Greedy line breaking at spaces; a word wider than a line breaks between characters."""
        lines: list[Line] = [[]]
        x = 0.0
        space: Piece | None = None
        for item in _words(found):
            if item is None:
                lines.append([])
                x, space = 0.0, None
                continue
            if isinstance(item, Span):
                space = Piece(item, 0, self.width(item, size)) if lines[-1] else None
                continue
            word = item
            total = sum(self.width(span, size) for span in word)
            gap = space.width if space else 0
            if lines[-1] and x + gap + total > width:
                lines.append([])
                x, space = 0.0, None
            if space is not None:
                space.x = x
                lines[-1].append(space)
                x += space.width
                space = None
            units = (
                word
                if total <= width
                else [u for s in word for u in ([s] if s.emoji else [Span(c, s.flags) for c in s.text])]
            )
            for unit in units:
                unit_width = self.width(unit, size)
                if lines[-1] and x + unit_width > width:
                    lines.append([])
                    x = 0.0
                lines[-1].append(Piece(unit, x, unit_width))
                x += unit_width
        return [self._joined(line, size) for line in lines]

    def _joined(self, line: Line, size: float) -> Line:
        """Neighbouring pieces of one style merged (kerning kept) and laid out again."""
        merged: list[Span] = []
        for piece in line:
            span = piece.span
            if merged and not span.emoji and not merged[-1].emoji and merged[-1].flags == span.flags:
                merged[-1] = Span(merged[-1].text + span.text, span.flags)
            else:
                merged.append(span)
        joined: Line = []
        x = 0.0
        for span in merged:
            width = self.width(span, size)
            joined.append(Piece(span, x, width))
            x += width
        return joined

    def one_line(self, found: Sequence[Span], size: float, width: float) -> Line:
        """The first line that fits, with "…" when there was more."""
        flat = [Span(s.text.replace("\n", " "), s.flags, s.emoji) for s in found]
        ellipsis = Span("…", flat[0].flags if flat else frozenset())
        lines = self.wrap(flat, size, max(1.0, width - self.width(ellipsis, size)))
        line = lines[0] if lines else []
        if len(lines) > 1 and line:
            line.append(Piece(ellipsis, _right(line), self.width(ellipsis, size)))
        return line

    def bubble(self, message: Quoted, media: Image.Image | None, *, first: bool, last: bool) -> Bubble:
        text, marks = clean(message.text, message.entities, MAX_CHARS)
        if message.sticker and media is not None and not text:
            side = self.px(STICKER_BOX)
            scale = side / max(media.size)
            size = (max(1, round(media.width * scale)), max(1, round(media.height * scale)))
            return Bubble(message, first, last, *size, bare=True, media=size)
        inner = self.px(TEXT_WIDTH)
        bubble = Bubble(message, first, last, 0, 0)
        height = 0
        if first:
            bubble.name = self.one_line(
                spans(message.name, [frozenset({"bold"})] * len(message.name)), NAME_SIZE, inner
            )
            bubble.content = max(bubble.content, math.ceil(_right(bubble.name)))
            height += self.line_height(NAME_SIZE)
        if message.reply is not None:
            reply = message.reply
            room = inner - self.px(13)
            name_line = self.one_line(
                spans(reply.name, [frozenset({"bold"})] * len(reply.name)), REPLY_SIZE, room
            )
            text_line = self.one_line(spans(clean(reply.text, (), 200)[0]), REPLY_SIZE, room)
            bubble.reply = (name_line, text_line)
            widest = max(_right(name_line), _right(text_line))
            bubble.content = max(bubble.content, math.ceil(widest) + self.px(13))
            height += self.line_height(REPLY_SIZE) * 2 + self.px(8) + self.px(6)
        if media is not None:
            box_w, box_h = self.px(MEDIA_BOX[0]), self.px(MEDIA_BOX[1])
            scale = min(box_w / media.width, box_h / media.height)
            bubble.media = (max(1, round(media.width * scale)), max(1, round(media.height * scale)))
            bubble.content = max(bubble.content, bubble.media[0])
            height += bubble.media[1] + (self.px(6) if text else 0)
        if text:
            bubble.lines = self.wrap(spans(text, marks), TEXT_SIZE, inner)
            bubble.content = max(bubble.content, *(math.ceil(_right(line)) for line in bubble.lines))
            height += self.line_height(TEXT_SIZE) * len(bubble.lines)
        bubble.content = max(bubble.content, self.px(24))
        bubble.width = bubble.content + 2 * self.px(PAD_X)
        bubble.height = max(height, self.line_height(TEXT_SIZE)) + 2 * self.px(PAD_Y)
        return bubble

    def layout(
        self, messages: Sequence[Quoted], media: Sequence[Image.Image | None]
    ) -> tuple[list[Bubble], tuple[int, int]]:
        bubbles: list[Bubble] = []
        for index, message in enumerate(messages):
            first = index == 0 or messages[index - 1].peer != message.peer
            last = index == len(messages) - 1 or messages[index + 1].peer != message.peer
            bubbles.append(self.bubble(message, media[index], first=first, last=last))
        left = self.px(MARGIN) + self.px(AVATAR) + self.px(GAP)
        y = self.px(MARGIN)
        for bubble in bubbles:
            if bubble.last:  # the avatar is never taller than its run
                y += max(0, self.px(AVATAR) - _run_height(bubbles, bubble, self))
            bubble.x, bubble.y = left, y
            y += bubble.height + (self.px(RUN_GAP) if bubble.last else self.px(STACK_GAP))
        width = left + max(b.width for b in bubbles) + self.px(MARGIN)
        return bubbles, (width, y - self.px(RUN_GAP) + self.px(MARGIN))

    # Drawing

    def paint(
        self,
        bubbles: Sequence[Bubble],
        size: tuple[int, int],
        media: Sequence[Image.Image | None],
        avatars: Sequence[Image.Image | None],
    ) -> Image.Image:
        canvas = Image.new("RGBA", size)
        for index, bubble in enumerate(bubbles):
            picture = media[index]
            if bubble.bare and picture is not None and bubble.media:
                canvas.alpha_composite(
                    picture.resize(bubble.media, Image.Resampling.LANCZOS), (bubble.x, bubble.y)
                )
            else:
                self._bubble(canvas, bubble, picture)
            if bubble.last:
                self._avatar(canvas, bubble, avatars[index])
        return canvas

    def _bubble(self, canvas: Image.Image, bubble: Bubble, picture: Image.Image | None) -> None:
        theme = self.theme
        mask = _shape(
            (bubble.width, bubble.height),
            self.px(RADIUS),
            self.px(TIGHT),
            (not bubble.first, False, False, True),
        )
        layer = Image.new("RGBA", mask.size, (*theme.bubble, 0))
        layer.putalpha(mask)
        canvas.alpha_composite(layer, (bubble.x, bubble.y))
        draw = ImageDraw.Draw(canvas)
        left, y = bubble.x + self.px(PAD_X), bubble.y + self.px(PAD_Y)
        message = bubble.message
        if bubble.name:
            self._line(canvas, draw, bubble.name, left, y, NAME_SIZE, theme.name(message.peer))
            y += self.line_height(NAME_SIZE)
        if bubble.reply is not None and message.reply is not None:
            colour = theme.name(message.reply.peer)
            height = self.line_height(REPLY_SIZE) * 2 + self.px(8)
            box = (left, y, left + bubble.content - 1, y + height - 1)
            draw.rounded_rectangle(box, radius=self.px(4), fill=colour)
            inset = (left + self.px(3), y, box[2], box[3])
            draw.rounded_rectangle(
                inset,
                radius=self.px(4),
                fill=mix(colour, theme.bubble, 0.15),
                corners=(False, True, True, False),
            )
            name_line, text_line = bubble.reply
            self._line(canvas, draw, name_line, left + self.px(9), y + self.px(4), REPLY_SIZE, colour)
            self._line(
                canvas,
                draw,
                text_line,
                left + self.px(9),
                y + self.px(4) + self.line_height(REPLY_SIZE),
                REPLY_SIZE,
            )
            y += height + self.px(6)
        if picture is not None and bubble.media is not None:
            shown = picture.resize(bubble.media, Image.Resampling.LANCZOS)
            rounded = _shape(bubble.media, self.px(8), self.px(8), (False, False, False, False))
            shown.putalpha(ImageChops.multiply(shown.getchannel("A"), rounded))
            canvas.alpha_composite(shown, (left, y))
            y += bubble.media[1] + self.px(6)
        for line in bubble.lines:
            self._line(canvas, draw, line, left, y, TEXT_SIZE)
            y += self.line_height(TEXT_SIZE)

    def _line(
        self,
        canvas: Image.Image,
        draw: ImageDraw.ImageDraw,
        line: Line,
        left: int,
        top: int,
        size: float,
        colour: RGB | None = None,
    ) -> None:
        theme = self.theme
        height = self.line_height(size)
        ascent, descent = self.font(frozenset(), size).getmetrics()
        baseline = top + (height - ascent - descent) / 2 + ascent
        for piece in line:
            x, flags = left + piece.x, piece.span.flags
            if "spoiler" in flags:
                self._speckle(draw, x, top, piece.width, height)
                continue
            fill = colour or (
                theme.link if "link" in flags else theme.code if "mono" in flags else theme.text
            )
            box = self.emoji_box(piece.span.text, size) if piece.span.emoji else None
            if box and (glyph := _emoji_image(piece.span.text, *box)):
                canvas.alpha_composite(glyph, (round(x), round(baseline - box[1] * 0.84)))
            else:
                font = self.font(flags, size)
                draw.text((x, baseline), piece.span.text, font=font, fill=fill, anchor="ls")
            stroke = self.px(1)
            if "underline" in flags:
                y = baseline + self.px(2)
                draw.line([(x, y), (x + piece.width, y)], fill=fill, width=stroke)
            if "strike" in flags:
                y = baseline - ascent * 0.3
                draw.line([(x, y), (x + piece.width, y)], fill=fill, width=stroke)

    def _speckle(self, draw: ImageDraw.ImageDraw, x: float, top: int, width: float, height: int) -> None:
        """A spoiler as Telegram hides it: a cloud of dots instead of the text (the same every time)."""
        theme = self.theme
        dice = random.Random(f"{x:.0f}:{top}:{width:.0f}")
        dot = max(1, self.px(1.3))
        for _ in range(max(4, int(width * height / (self.px(3.2) ** 2)))):
            dx, dy = x + dice.random() * width, top + height * (0.15 + dice.random() * 0.7)
            fill = mix(theme.text, theme.bubble, dice.uniform(0.3, 0.95))
            draw.ellipse((dx, dy, dx + dot, dy + dot), fill=fill)

    def _avatar(self, canvas: Image.Image, bubble: Bubble, picture: Image.Image | None) -> None:
        side = self.px(AVATAR)
        if picture is not None:
            crop = min(picture.size)
            left, top = (picture.width - crop) // 2, (picture.height - crop) // 2
            face = picture.crop((left, top, left + crop, top + crop)).resize(
                (side, side), Image.Resampling.LANCZOS
            )
        else:
            face = self._initials(bubble.message, side)
        circle = Image.new("L", (side, side))
        ImageDraw.Draw(circle).ellipse((0, 0, side - 1, side - 1), fill=255)
        face.putalpha(ImageChops.multiply(face.getchannel("A"), circle))
        canvas.alpha_composite(face, (self.px(MARGIN), bubble.y + bubble.height - side))

    def _initials(self, message: Quoted, side: int) -> Image.Image:
        """Telegram's stand-in for a missing photo: initials on the peer colour, lighter at the top."""
        base = _rgb(AVATARS[abs(message.peer) % len(AVATARS)])
        top = Image.new("RGBA", (side, side), (*mix((255, 255, 255), base, 0.25), 255))
        face = Image.new("RGBA", (side, side), (*base, 255))
        face = Image.composite(face, top, Image.linear_gradient("L").resize((side, side)))
        letters = initials(message.name)
        font = self.font(frozenset({"bold"}), AVATAR * 0.4)
        ImageDraw.Draw(face).text((side / 2, side / 2), letters, font=font, fill="white", anchor="mm")
        return face


def initials(name: str) -> str:
    letters = [word[0] for word in name.split() if word[0].isalnum()]
    return "".join(letters[:2]).upper() or "?"


def _right(line: Line) -> float:
    return line[-1].x + line[-1].width if line else 0.0


def _run_height(bubbles: Sequence[Bubble], last: Bubble, setter: Typesetter) -> int:
    """How tall the run ending with ``last`` is (bubbles and the gaps between them)."""
    end = next(i for i, b in enumerate(bubbles) if b is last)
    start = end
    while start > 0 and not bubbles[start - 1].last:
        start -= 1
    run = bubbles[start : end + 1]
    return sum(b.height for b in run) + setter.px(STACK_GAP) * (len(run) - 1)


def _shape(
    size: tuple[int, int], radius: int, tight: int, tight_corners: tuple[bool, bool, bool, bool]
) -> Image.Image:
    """A rounded-rectangle mask; ``tight_corners`` (top left, top right, bottom right, bottom left) get ``tight``."""
    width, height = size
    radius = min(radius, width // 2, height // 2)
    mask = Image.new("L", size)
    corners = (not tight_corners[0], not tight_corners[1], not tight_corners[2], not tight_corners[3])
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, width - 1, height - 1), radius=radius, fill=255, corners=corners
    )
    if any(tight_corners):
        small = Image.new("L", size)
        ImageDraw.Draw(small).rounded_rectangle(
            (0, 0, width - 1, height - 1), radius=min(tight, width // 2, height // 2), fill=255
        )
        mask = ImageChops.multiply(mask, small)
    return mask


def _picture(data: bytes | None, max_pixels: int) -> Image.Image | None:
    if not data:
        return None
    try:
        return open_image(data, max_pixels)
    except EffectError:
        return None


def _within_budget(messages: Sequence[Quoted]) -> list[Quoted]:
    """At most ``MAX_MESSAGES``, and text cut so the whole quote stays within ``MAX_TOTAL_CHARS``."""
    kept: list[Quoted] = []
    left = MAX_TOTAL_CHARS
    for message in messages[:MAX_MESSAGES]:
        if len(message.text) > left:
            message = replace(message, text=message.text[: max(left - 1, 0)] + "…")
        left = max(0, left - len(message.text))
        kept.append(message)
    return kept


def render(
    messages: Sequence[Quoted],
    *,
    output: Output = "sticker",
    bubble: str | None = None,
    max_pixels: int = DEFAULTS.max_pixels,
) -> bytes:
    """The quote as a WebP sticker (512 px, under 512 KB, transparent) or a transparent PNG."""
    messages = _within_budget(messages)
    if not messages:
        raise EffectError("nothing to quote")
    theme = Theme.of(bubble)
    media = [_picture(m.media, max_pixels) for m in messages]
    avatars = _avatars(messages, max_pixels)
    _, (width, height) = Typesetter(1, theme).layout(messages, media)
    target = (
        STICKER_SIDE / max(width, height) if output == "sticker" else min(2.0, PNG_SIDE / max(width, height))
    )
    setter = Typesetter(min(max(2 * target, 1.0), 8.0), theme)
    bubbles, size = setter.layout(messages, media)
    check_dimensions(*size, max_pixels)
    canvas = setter.paint(bubbles, size, media, avatars)
    if output == "sticker":
        return encode_sticker(canvas)
    ratio = target / setter.scale
    final = canvas.resize(
        (max(1, round(size[0] * ratio)), max(1, round(size[1] * ratio))), Image.Resampling.LANCZOS
    )
    buffer = io.BytesIO()
    final.save(buffer, format="PNG")
    return buffer.getvalue()


def _avatars(messages: Sequence[Quoted], max_pixels: int) -> list[Image.Image | None]:
    """Each run's avatar at its last message: the first of the run's pictures that opens."""
    avatars: list[Image.Image | None] = []
    start = 0
    for index, message in enumerate(messages):
        if index and messages[index - 1].peer != message.peer:
            start = index
        last = index == len(messages) - 1 or messages[index + 1].peer != message.peer
        candidates = (_picture(m.avatar, max_pixels) for m in messages[start : index + 1]) if last else ()
        avatars.append(next((picture for picture in candidates if picture is not None), None))
    return avatars
