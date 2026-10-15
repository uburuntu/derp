"""Telegram text measures and splitting.

Plain messages are limited in UTF-16 code units (4096); rich messages allow
32768 characters. Splits prefer paragraph, then line, then word boundaries.
"""

from collections.abc import Callable

PLAIN_LIMIT = 4096
CAPTION_LIMIT = 1024
RICH_LIMIT = 32_768


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
