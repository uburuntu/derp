"""Meme captions drawn with Pillow: demotivator frames, Lobster overlays and white top panels.

Captions are measured and wrapped to fit (long words break, line breaks are
kept, the font shrinks until the text fits, and as a last resort the whole
text layer is scaled). ``caption_frame`` gives the geometry and a transparent
overlay that images and video frames share, so user text is only ever drawn by
Pillow and never handed to ffmpeg.
"""

import io
import math
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Literal

from PIL import Image, ImageDraw, ImageFont, ImageOps

from derp.tools.workers import DEFAULTS, EffectError, check_dimensions

Style = Literal["demotivator", "lobster", "panel"]
FONTS = Path(__file__).with_name("fonts")
LOBSTER = FONTS / "Lobster-Regular.ttf"
SERIF = FONTS / "LiberationSerif-Regular.ttf"
SANS = FONTS / "LiberationSans-Regular.ttf"
MAX_SIDE = 1600  # media is scaled down to this before captioning
MIN_WIDTH, MIN_HEIGHT = 320, 240
MAX_TEXT = 300


@cache
def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


@dataclass(slots=True)
class Caption:
    text: str
    font: ImageFont.FreeTypeFont
    bounds: tuple[int, int, int, int]
    spacing: int
    stroke: int = 0
    scale: float = 1

    @property
    def width(self) -> int:
        return max(1, round((self.bounds[2] - self.bounds[0]) * self.scale))

    @property
    def height(self) -> int:
        return max(1, round((self.bounds[3] - self.bounds[1]) * self.scale))

    def draw(self, image: Image.Image, left: int, top: int, color: str = "white") -> None:
        if self.scale == 1:
            self._draw(image, left, top, color)
            return
        raw = (max(1, self.bounds[2] - self.bounds[0]), max(1, self.bounds[3] - self.bounds[1]))
        layer = Image.new("RGBA", raw)
        self._draw(layer, 0, 0, color)
        scaled = layer.resize((self.width, self.height), Image.Resampling.LANCZOS)
        image.alpha_composite(scaled, (left, top))

    def _draw(self, image: Image.Image, left: int, top: int, color: str) -> None:
        ImageDraw.Draw(image).multiline_text(
            (left - self.bounds[0], top - self.bounds[1]),
            self.text,
            font=self.font,
            spacing=self.spacing,
            align="center",
            fill=color,
            stroke_width=self.stroke,
            stroke_fill="black",
        )


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int, stroke: int) -> str:
    def fits(value: str) -> bool:
        left, _, right, _ = font.getbbox(value, stroke_width=stroke)
        return right - left <= width

    def longest_prefix(word: str) -> int:
        low, high = 1, len(word)
        while low < high:
            middle = (low + high + 1) // 2
            if fits(word[:middle]):
                low = middle
            else:
                high = middle - 1
        return low

    lines: list[str] = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split():
            if line and fits(f"{line} {word}"):
                line = f"{line} {word}"
                continue
            if line:
                lines.append(line)
            while not fits(word) and len(word) > 1:
                cut = longest_prefix(word)
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    return "\n".join(lines)


def fit_caption(
    text: str, font_path: Path, width: int, height: int, preferred: int, stroke: int = 0
) -> Caption:
    text = re.sub(r"\n\s*\n(\s*\n)+", "\n\n", text.replace("\r\n", "\n").strip())[:MAX_TEXT]
    if not text:
        raise EffectError("the caption is empty")

    def layout(size: int) -> Caption:
        font = _font(font_path, size)
        spacing = max(1, size // 5)
        outline = min(stroke, size // 8)
        wrapped = _wrap(text, font, width, outline)
        box = ImageDraw.Draw(Image.new("L", (1, 1))).multiline_textbbox(
            (0, 0), wrapped, font=font, spacing=spacing, align="center", stroke_width=outline
        )
        bounds = (math.floor(box[0]), math.floor(box[1]), math.ceil(box[2]), math.ceil(box[3]))
        return Caption(wrapped, font, bounds, spacing, outline)

    def fitting(caption: Caption) -> bool:
        return caption.width <= width and caption.height <= height

    best = layout(preferred)
    if not fitting(best):
        low, high, found = 1, preferred - 1, None
        while low <= high:
            size = (low + high) // 2
            if fitting(candidate := layout(size)):
                found, low = candidate, size + 1
            else:
                high = size - 1
        if found is None:  # too many lines for any font size: scale the whole layer instead
            found = layout(1)
            found.scale = min(1, width / found.width, height / found.height)
        best = found
    # Centre the visible ink rather than the font's metric box.
    raw = (max(1, best.bounds[2] - best.bounds[0]), max(1, best.bounds[3] - best.bounds[1]))
    mask = Image.new("RGBA", raw)
    best._draw(mask, 0, 0, "white")
    if ink := mask.getchannel("A").getbbox():
        x, y = best.bounds[:2]
        best.bounds = (x + ink[0], y + ink[1], x + ink[2], y + ink[3])
    return best


def media_size(width: int, height: int) -> tuple[int, int]:
    """Display size for checked media: at most MAX_SIDE, small inputs scaled up, even sides."""
    scale = min(MAX_SIDE / max(width, height), 1.0)
    if max(width, height) * scale < MIN_WIDTH:
        scale = MIN_WIDTH / max(width, height)
    return max(2, round(width * scale / 2) * 2), max(2, round(height * scale / 2) * 2)


@dataclass(slots=True)
class Frame:
    overlay: Image.Image  # RGBA over a black canvas; transparent where the media shows through
    position: tuple[int, int]  # where the media's top-left goes on the overlay


def _panel(width: int, text: str, border: int, *, white: bool) -> Image.Image:
    caption = fit_caption(
        text,
        SANS if white else SERIF,
        width - border * 2,
        min(1200, max(240, width)),
        min(96, round(width * 0.075)),
    )
    height = caption.height + border * 2
    height += height % 2
    panel = Image.new("RGBA", (width, height), "white" if white else "black")
    caption.draw(
        panel, (width - caption.width) // 2, (height - caption.height) // 2, "black" if white else "white"
    )
    return panel


def caption_frame(size: tuple[int, int], text: str, style: Style) -> Frame:
    width, height = max(MIN_WIDTH, size[0]), max(MIN_HEIGHT, size[1])
    offset = ((width - size[0]) // 2, (height - size[1]) // 2)
    if style == "lobster":
        margin, bottom = max(12, round(width * 0.04)), max(12, round(height * 0.08))
        caption = fit_caption(
            text,
            LOBSTER,
            width - margin * 2,
            height - bottom - max(12, height // 5),
            min(100, round(0.0669 * width + 4.2772)),
            stroke=1,
        )
        overlay = Image.new("RGBA", (width, height))
        caption.draw(overlay, (width - caption.width) // 2, height - bottom - caption.height)
        return Frame(overlay, offset)
    if style == "panel":
        panel = _panel(width, text, max(12, round(width * 0.04)), white=True)
        overlay = Image.new("RGBA", (width, height + panel.height))
        overlay.paste(panel, (0, 0))
        return Frame(overlay, (offset[0], offset[1] + panel.height))
    border = max(round((width + 12) * 0.07), 20)
    frame_width = width + 12 + border * 2
    panel_top = border + height + 12
    panel = _panel(frame_width, text, border, white=False)
    overlay = Image.new("RGBA", (frame_width, panel_top + panel.height), "black")
    ImageDraw.Draw(overlay).rectangle(
        (border, border, frame_width - border - 1, panel_top - 1), outline="white", width=3
    )
    left, top = border + 6 + offset[0], border + 6 + offset[1]
    overlay.paste((0, 0, 0, 0), (left, top, left + size[0], top + size[1]))
    overlay.paste(panel, (0, panel_top))
    return Frame(overlay, (left, top))


def open_image(data: bytes, max_pixels: int = DEFAULTS.max_pixels) -> Image.Image:
    """Decode an image within the pixel budget, upright and in RGBA."""
    try:
        source = Image.open(io.BytesIO(data))
        check_dimensions(*source.size, max_pixels)
        image = ImageOps.exif_transpose(source).convert("RGBA")
    except (OSError, ValueError, Image.DecompressionBombError) as error:
        if isinstance(error, EffectError):
            raise
        raise EffectError("not a readable image") from None
    return image


def caption_image(data: bytes, text: str, style: Style, max_pixels: int = DEFAULTS.max_pixels) -> bytes:
    """The captioned picture as JPEG."""
    image = open_image(data, max_pixels)
    image = image.resize(media_size(*image.size), Image.Resampling.LANCZOS)
    frame = caption_frame(image.size, text, style)
    result = Image.new("RGBA", frame.overlay.size, "black")
    result.alpha_composite(image, frame.position)
    result.alpha_composite(frame.overlay)
    return encode_jpeg(result)


def encode_jpeg(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def mirror(data: bytes, max_pixels: int = DEFAULTS.max_pixels) -> bytes:
    image = open_image(data, max_pixels)
    return encode_jpeg(ImageOps.mirror(image.resize(media_size(*image.size), Image.Resampling.LANCZOS)))


STICKER_SIDE = 512
STICKER_BYTES = 512 * 1024


def sticker(data: bytes, max_pixels: int = DEFAULTS.max_pixels) -> bytes:
    """A static sticker: WebP with transparency kept, the longer side exactly 512 px, under 512 KB."""
    image = open_image(data, max_pixels)
    scale = STICKER_SIDE / max(image.size)
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    image = image.resize(size, Image.Resampling.LANCZOS)
    for quality in (90, 75, 60, 40):
        buffer = io.BytesIO()
        image.save(buffer, format="WEBP", quality=quality, method=6)
        if buffer.tell() <= STICKER_BYTES:
            break
    return buffer.getvalue()
