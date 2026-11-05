"""Price charts for the ``market`` tool: a quote's series over its period as a PNG, drawn with Pillow.

A dark card: the symbol and the period's move on top (green up, red down), the line with a soft fill under
it, the price scale on the right with the last price marked, dates along the bottom in the chat's time zone
and the source in the corner. A prediction market with several likely outcomes gets a line for each (up to
four) and a legend with their chances instead. Drawn at twice the size and scaled down, so lines and text come
out smooth. Pure: bytes in, bytes out, no network.
"""

import io
import itertools
import math
from datetime import UTC, datetime, tzinfo
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from derp.tools.markets import Quote, chance_text, change, price_text

FONTS = Path(__file__).with_name("fonts")
SPARE = (
    FONTS / "LiberationSans-Regular.ttf"
)  # arrows and math signs Noto Sans lacks (Polymarket's "↑ 82,500")
WIDTH, HEIGHT, SCALE = 1200, 675, 2
LEFT, RIGHT, TOP, BOTTOM = 40, 150, 130, 92  # the plot's margins
LEGEND = 40  # extra room above the plot for a legend
BACKGROUND, GRID, TEXT, MUTED = (16, 18, 24), (42, 46, 56), (232, 236, 242), (138, 147, 162)
UP, DOWN = (34, 197, 94), (239, 68, 68)
PALETTE = ((59, 130, 246), (245, 158, 11), (168, 85, 247), (236, 72, 153))  # outcome lines, likeliest first
type Points = list[tuple[float, float]]


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(
        str(FONTS / ("NotoSans-Bold.ttf" if bold else "NotoSans-Regular.ttf")), size * SCALE
    )


def _write(
    draw: ImageDraw.ImageDraw,
    at: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, ...],
    anchor: str = "la",
) -> None:
    """Left-aligned text, the characters ``font`` has no glyph for drawn in the spare font."""
    spare = ImageFont.truetype(str(SPARE), font.size)
    x, y = at
    for lacking, run in itertools.groupby(text, lambda c: not c.isspace() and not font.getmask(c).getbbox()):
        piece, face = "".join(run), spare if lacking else font
        draw.text((x, y), piece, font=face, fill=fill, anchor=anchor)
        x += draw.textlength(piece, font=face)


def _fit(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: float) -> str:
    """``text`` cut with an ellipsis to fit ``width``."""
    while text and draw.textlength(text, font=font) > width:
        text = text[:-2] + "…" if len(text) > 2 else ""
    return text


def _ticks(low: float, high: float) -> list[float]:
    """Round levels across the range, about four: steps of 1, 2 or 5 times a power of ten."""
    span = high - low or abs(high) or 1
    power = 10 ** math.floor(math.log10(span / 4))
    step = min((m * power for m in (1, 2, 5, 10)), key=lambda step: abs(span / step - 4))
    first = math.ceil(low / step) * step
    return [first + i * step for i in range(int((high - first) / step) + 1)]


def _dates(start: float, end: float, tz: tzinfo, count: int = 5) -> list[tuple[float, str]]:
    span = end - start
    shape = "%H:%M" if span <= 2 * 86400 else "%d %b" if span <= 200 * 86400 else "%b %Y"
    moments = [start + span * i / (count - 1) for i in range(count)]
    return [(at, datetime.fromtimestamp(at, UTC).astimezone(tz).strftime(shape)) for at in moments]


def _spread(wanted: list[float], gap: float) -> list[float]:
    """Label heights moved apart just enough not to overlap, in their order."""
    order = sorted(range(len(wanted)), key=lambda i: wanted[i])
    placed = list(wanted)
    for previous, current in itertools.pairwise(order):
        placed[current] = max(placed[current], placed[previous] + gap)
    return placed


def render(found: Quote, period_label: str, tz: tzinfo) -> bytes:
    """The quote's series (or its outcome lines) as a chart; at least two points."""
    s = SCALE
    image = Image.new("RGB", (WIDTH * s, HEIGHT * s), BACKGROUND)
    draw = ImageDraw.Draw(image)
    lines: list[tuple[str, Points]] = [(label, points) for label, points in found.lines if points] or [
        (found.name, found.series)
    ]
    many = len(lines) > 1
    moved = change(found)
    color = DOWN if moved is not None and moved[0] < 0 else UP
    colors = list(PALETTE) if many else [color]
    level = chance_text if found.currency == "%" else price_text
    title, big, small, tiny = _font(40, bold=True), _font(34, bold=True), _font(22), _font(18)

    corner = "" if many else (moved[1] if moved is not None else "")
    corner_width = draw.textlength(corner, font=big)
    draw.text((WIDTH * s - LEFT * s - corner_width, 34 * s), corner, font=big, fill=color)
    _write(
        draw,
        (LEFT * s, 26 * s),
        _fit(draw, found.symbol, title, WIDTH * s - 3 * LEFT * s - corner_width),
        title,
        TEXT,
    )
    named = found.name if found.name != found.symbol and not many else ""
    under = " · ".join(part for part in (named, f"past {period_label}") if part)
    draw.text((LEFT * s, 82 * s), _fit(draw, under, small, WIDTH * s - 2 * LEFT * s), font=small, fill=MUTED)
    if many:  # the legend: each outcome and its chance now, in a row, cut only when the row doesn't fit
        entries = [f"{label} {level(points[-1][1])}" for label, points in lines]
        room = (WIDTH - 2 * LEFT) * s - (len(entries) - 1) * 36 * s
        widths = [draw.textlength(text, font=small) + 22 * s for text in entries]
        cap = max(widths)
        while sum(min(w, cap) for w in widths) > room:
            cap -= 4 * s
        cursor = LEFT * s
        for text, wide, shade in zip(entries, widths, colors, strict=False):
            draw.ellipse([cursor, 129 * s, cursor + 14 * s, 143 * s], fill=shade)
            _write(
                draw,
                (cursor + 22 * s, 136 * s),
                _fit(draw, text, small, min(wide, cap) - 22 * s),
                small,
                TEXT,
                "lm",
            )
            cursor += min(wide, cap) + 36 * s

    left, right, bottom = LEFT * s, (WIDTH - RIGHT) * s, (HEIGHT - BOTTOM) * s
    top = (TOP + (LEGEND if many else 0)) * s
    values = [value for _, points in lines for _, value in points]
    stamps = [at for _, points in lines for at, _ in points]
    start, end = min(stamps), max(stamps)
    low, high = min(values), max(values)
    pad = (high - low) * 0.08 or abs(high) * 0.01 or 1
    low, high = low - pad, high + pad

    def x(at: float) -> float:
        return left + (at - start) / ((end - start) or 1) * (right - left)

    def y(value: float) -> float:
        return bottom - (value - low) / (high - low) * (bottom - top)

    drawn = [[(x(at), y(value)) for at, value in points] for _, points in lines]
    heights = _spread(
        [points[-1][1] for points in drawn], 30 * s
    )  # each line's last value, marked on the scale
    for value in _ticks(low, high):  # the scale, its levels unlabeled where a mark covers them
        draw.line([(left, y(value)), (right, y(value))], fill=GRID, width=s)
        if all(abs(y(value) - height) > 26 * s for height in heights):
            draw.text((right + 14 * s, y(value)), level(value), font=tiny, fill=MUTED, anchor="lm")
    for at, label in _dates(start, end, tz):
        draw.text((x(at), bottom + 14 * s), label, font=tiny, fill=MUTED, anchor="ma")

    if not many:  # one line: a soft fill under it
        fill = Image.new("RGBA", image.size, (0, 0, 0, 0))
        ImageDraw.Draw(fill).polygon(
            [(drawn[0][0][0], bottom), *drawn[0], (drawn[0][-1][0], bottom)], fill=(*color, 46)
        )
        image.paste(fill, mask=fill)
        draw = ImageDraw.Draw(image)
    for points, shade in reversed(list(zip(drawn, colors, strict=False))):  # the likeliest drawn last, on top
        if len(points) > 1:
            draw.line(points, fill=shade, width=3 * s, joint="curve")
        draw.ellipse(
            [points[-1][0] - 6 * s, points[-1][1] - 6 * s, points[-1][0] + 6 * s, points[-1][1] + 6 * s],
            fill=shade,
        )

    for (_, points), height, shade in zip(lines, heights, colors, strict=False):
        label = level(points[-1][1] if many else found.price)
        box = draw.textbbox((right + 10 * s, height), label, font=tiny, anchor="lm")
        draw.rounded_rectangle(
            [box[0] - 6 * s, box[1] - 5 * s, box[2] + 6 * s, box[3] + 5 * s], radius=6 * s, fill=shade
        )
        draw.text((right + 10 * s, height), label, font=tiny, fill=BACKGROUND, anchor="lm")

    footer = found.source if found.currency == "%" else f"{found.source} · {found.currency}"
    draw.text(
        (LEFT * s, (HEIGHT - 26) * s),
        _fit(draw, footer, tiny, WIDTH * s - 2 * LEFT * s),
        font=tiny,
        fill=MUTED,
        anchor="ls",
    )

    out = io.BytesIO()
    image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS).save(out, format="PNG", optimize=True)
    return out.getvalue()
