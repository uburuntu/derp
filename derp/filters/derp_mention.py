"""Filter for messages that mention 'derp' or 'дерп'."""

import re

from aiogram.filters import BaseFilter
from aiogram.types import Message


class DerpMentionFilter(BaseFilter):
    """Filter for messages that mention 'derp' or 'дерп'."""

    async def __call__(self, message: Message) -> bool:
        text = message.text or message.caption
        if not text:
            return False

        # Case-insensitive check for "derp" in English or Russian
        pattern = r"\b(derp|дерп)\b"
        return bool(re.search(pattern, text, re.IGNORECASE))
