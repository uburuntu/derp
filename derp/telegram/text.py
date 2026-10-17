"""Telegram text measures, splitting and line breaks.

Plain messages are limited in UTF-16 code units (4096); rich messages allow
32768 characters. Splits prefer paragraph, then line, then word boundaries.
Rich Markdown follows GitHub Flavored Markdown, where a single newline inside a
paragraph is a soft break rendered as a space, and Telegram reads "#" at the start
of a block as a heading; ``as_written`` keeps both as written. Emoji are
compared plain (``plain_emoji``): Telegram's reaction list has no presentation
selectors, sticker tags often do, and models write either.
"""

import re
from collections.abc import Callable

PLAIN_LIMIT = 4096
CAPTION_LIMIT = 1024
RICH_LIMIT = 32_768


_FENCE_LINE = re.compile(r" {0,3}(```|~~~)")
# Telegram reads "#" at the start of a block as a heading even without the space GFM requires
# ("#3 chat", "#news"); a real heading ("# Title", "## Title") keeps its space.
_HASH_START = re.compile(r"^((?:\s*(?:[-*+]|\d{1,9}[.)])\s+|\s*>\s*)*\s*)#(?=[^#\s])")

_PUNCTUATION = re.compile(r"([!-/:-@\[-`{-~])")
# The emoji presentation selector (U+FE0F) and the skin-tone modifiers: spellings of the same emoji.
_EMOJI_VARIANTS = str.maketrans("", "", "\ufe0f" + "".join(map(chr, range(0x1F3FB, 0x1F400))))


def plain_emoji(emoji: str) -> str:
    """One spelling per emoji, as Telegram's reaction list writes them: "❤️" and "👍🏽" become "❤" and "👍"."""
    return emoji.strip().translate(_EMOJI_VARIANTS)


def literal(text: str) -> str:
    """Untrusted text shown as-is inside trusted rich Markdown: every ASCII punctuation mark escaped."""
    return _PUNCTUATION.sub(r"\\\1", text)


_CODE_SPAN = re.compile(r"(`[^`\n]*`)")


def _prose_mask(lines: list[str]) -> list[bool]:
    """Which lines are prose: not code-block fences or contents, not inside a ``$$ … $$`` formula.

    A formula's opening line belongs to the formula; its closing line is prose again (text may follow).
    """
    mask: list[bool] = []
    fence: str | None = None
    math = False
    paired = sum(line.count("$$") for line in lines) % 2 == 0  # a stray "$$$" must not swallow the rest
    for line in lines:
        if marker := _FENCE_LINE.match(line):
            fence = None if fence == marker.group(1) else fence or marker.group(1)
            mask.append(False)
        elif fence is not None:
            mask.append(False)
        elif paired and line.count("$$") % 2:
            math = not math
            mask.append(not math)
        else:
            mask.append(not math)
    return mask


def map_prose(markdown: str, change: Callable[[str], str]) -> str:
    """``markdown`` with ``change`` applied to its prose: code blocks, code spans and formulas untouched."""
    lines = markdown.split("\n")
    for index, prose in enumerate(_prose_mask(lines)):
        if prose:
            parts = _CODE_SPAN.split(lines[index])
            lines[index] = "".join(part if odd % 2 else change(part) for odd, part in enumerate(parts))
    return "\n".join(lines)


def as_written(markdown: str) -> str:
    """Markdown that Telegram renders as written: every line on its own line, "#3" not a heading.

    A line break inside a paragraph becomes a hard break (two trailing spaces); a "#" that starts a
    block without a space is escaped (a hashtag stays a hashtag). Code blocks and ``$$ … $$``
    formulas are left as written: their source is raw (LaTeX for formulas).
    """
    lines = markdown.split("\n")
    last = len(lines) - 1
    for index, prose in enumerate(_prose_mask(lines)):
        if not prose:
            continue
        line = lines[index] = _HASH_START.sub(r"\1\\#", lines[index])
        if index == last or not line.strip() or not lines[index + 1].strip():
            continue
        if _FENCE_LINE.match(lines[index + 1]) or line.endswith(("  ", "\\")):
            continue
        lines[index] = line + "  "
    return "\n".join(lines)


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_text(text: str, limit: int = PLAIN_LIMIT, measure: Callable[[str], int] = utf16_len) -> list[str]:
    if measure(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    rest = text
    while measure(rest) > limit:
        cut = _cut(rest, limit, measure)
        chunk, rest = rest[:cut].rstrip(), rest[cut:].lstrip("\n")  # keep indentation
        if chunk:
            chunks.append(chunk)
    if rest:
        chunks.append(rest)
    return chunks


def _cut(text: str, limit: int, measure: Callable[[str], int]) -> int:
    # Largest prefix that fits, found by binary search over code points.
    low, high = 1, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if measure(text[:mid]) <= limit:
            low = mid
        else:
            high = mid - 1
    window = text[:low]
    for separator in ("\n\n", "\n", " "):
        index = window.rfind(separator)
        if index > low // 2:
            return index + len(separator)
    return low


def truncate(text: str, limit: int, ellipsis: str = "…", measure: Callable[[str], int] = utf16_len) -> str:
    if measure(text) <= limit:
        return text
    budget = limit - measure(ellipsis)
    return text[: _cut(text, budget, lambda s: measure(s))].rstrip() + ellipsis if budget > 0 else ellipsis
