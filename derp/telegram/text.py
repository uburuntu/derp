"""Telegram text measures, splitting and line breaks.

Plain messages are limited in UTF-16 code units (4096); rich messages allow
32768 characters. Splits prefer paragraph, then line, then word boundaries.
Rich Markdown follows GitHub Flavored Markdown, where a single newline inside a
paragraph is a soft break rendered as a space, and Telegram reads "#" at the start
of a block as a heading; ``as_written`` keeps both as written. Emoji are
compared plain (``plain_emoji``): Telegram's reaction list has no presentation
selectors, sticker tags often do, and models write either. ``visible`` reads Markdown back as
the text and entities Telegram shows.
"""

import re
from collections.abc import Callable
from typing import Any

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


_HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.*?)(?:\s+#+)?\s*$")
_RULE = re.compile(r"^ {0,3}([-*_])(?:\s*\1){2,}\s*$")
_QUOTE_MARK = re.compile(r"^\s*>\s?")
_BULLET = re.compile(r"^(\s*)[-*+]\s+(?:\[([ xX])\]\s+)?")
_INLINE = re.compile(
    r"`(?P<code>[^`\n]+)`"
    r"|\\(?P<escaped>[!-/:-@\[-`{-~])"
    r"|!?\[(?P<label>[^\]\n]*)\]\((?P<url>[^)\s]*)(?:\s+\"[^\"\n]*\")?\)"
    r"|(?P<mark>\*\*|__|~~|\|\||==)(?P<inner>(?:(?!(?P=mark)).)+)(?P=mark)"
    r"|(?<![\w*\\])\*(?P<star>[^*\s](?:[^*\n]*[^*\s])?)\*(?![\w*])"
    r"|(?<![\w\\])_(?P<under>[^_\s](?:[^_\n]*[^_\s])?)_(?!\w)"
)
_MARKS = {"**": "bold", "__": "bold", "~~": "strikethrough", "||": "spoiler", "==": ""}


class _Shown:
    """Visible text being built, with its entities (UTF-16 offsets, as Telegram counts them)."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.size = 0
        self.entities: list[dict[str, Any]] = []

    def add(self, text: str) -> None:
        self.parts.append(text)
        self.size += utf16_len(text)

    def mark(self, kind: str, start: int, **extra: str) -> None:
        if kind and self.size > start:
            self.entities.append({"type": kind, "offset": start, "length": self.size - start, **extra})

    def inline(self, text: str) -> None:
        at = 0
        for match in _INLINE.finditer(text):
            self.add(text[at : match.start()])
            at, start = match.end(), self.size
            if (code := match["code"]) is not None:
                self.add(code)
                self.mark("code", start)
            elif (char := match["escaped"]) is not None:
                self.add(char)
            elif match["label"] is not None:
                self.inline(match["label"])
                self.mark("text_link", start, url=match["url"])
            elif mark := match["mark"]:
                self.inline(match["inner"])
                self.mark(_MARKS[mark], start)
            else:
                self.inline(match["star"] or match["under"])
                self.mark("italic", start)
        self.add(text[at:])

    def block(self, line: str) -> None:
        if _RULE.match(line):
            self.add("———")
        elif heading := _HEADING.match(line):
            start = self.size
            self.inline(heading[1])
            self.mark("bold", start)
        else:
            line = _QUOTE_MARK.sub("", line)
            if bullet := _BULLET.match(line):
                box = {" ": "☐ ", "x": "☑ ", "X": "☑ "}.get(bullet[2] or "", "• ")
                self.add(bullet[1] + box)
                line = line[bullet.end() :]
            self.inline(line)


def visible(markdown: str) -> tuple[str, list[dict[str, Any]]]:
    """What Telegram shows for rich Markdown Derp sent: the text and its entities.

    Derp's messages are stored as the Markdown it wrote; whatever draws them as the chat saw them
    (quote stickers) reads them through here. Inline styles, code spans and links become entities,
    headings are bold, list markers bullets, code blocks ``pre``; formulas and tables stay as written,
    and a "#" without a space ("#3") is no heading, as ``as_written`` keeps it.
    """
    lines = markdown.split("\n")
    shown = _Shown()
    fence: str | None = None
    pre = -1  # where the open code block's text starts (-1: before its first line)
    first = True
    for line, prose in zip(lines, _prose_mask(lines), strict=True):
        if not prose and (marker := _FENCE_LINE.match(line)) and fence in (None, marker.group(1)):
            if fence is not None and pre >= 0:
                shown.mark("pre", pre)
            fence, pre = (None if fence else marker.group(1)), -1
            continue
        if not first:
            shown.add("\n")
        first = False
        if fence is not None and pre < 0:
            pre = shown.size
        if prose:
            shown.block(line)
        else:
            shown.add(line)  # code or a formula: raw
    if fence is not None and pre >= 0:
        shown.mark("pre", pre)
    shown.entities.sort(key=lambda e: (e["offset"], -e["length"]))
    return "".join(shown.parts), shown.entities


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
