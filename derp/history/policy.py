"""Pure capture and visibility policy for Telegram conversation history."""

from __future__ import annotations

import re
from typing import Final

from aiogram.types import Message

from derp.history.snapshot import CaptureKind

CONTEXT_NOTICE_VERSION: Final = 1

_DERP_NAME = re.compile(r"\b(?:derp|дерп)\b", re.IGNORECASE)
_SENSITIVE_CONTENT_TYPES: Final = frozenset(
    {
        "connected_website",
        "contact",
        "location",
        "passport_data",
        "successful_payment",
        "venue",
        "web_app_data",
    }
)
_CONVERSATIONAL_CONTENT_TYPES: Final = frozenset(
    {
        "animation",
        "audio",
        "document",
        "live_photo",
        "paid_media",
        "photo",
        "sticker",
        "text",
        "video",
        "video_note",
        "voice",
    }
)


def capture_kind_for_message(
    message: Message,
    *,
    ambient_enabled: bool,
    bot_id: int,
    bot_username: str,
) -> CaptureKind | None:
    """Classify a conversational message or reject it from durable history."""
    if message.content_type in _SENSITIVE_CONTENT_TYPES:
        return None
    if message.content_type not in _CONVERSATIONAL_CONTENT_TYPES:
        return None
    if message.chat.type == "private" or is_explicit_invocation(
        message,
        bot_id=bot_id,
        bot_username=bot_username,
    ):
        return CaptureKind.EXPLICIT
    if ambient_enabled:
        return CaptureKind.AMBIENT
    return None


def is_explicit_invocation(
    message: Message,
    *,
    bot_id: int,
    bot_username: str,
) -> bool:
    """Recognize commands, replies, usernames, and configured whole-word names."""
    text = message.text or message.caption or ""
    if text.lstrip().startswith("/"):
        return True
    if (
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == bot_id
    ):
        return True
    normalized_username = bot_username.removeprefix("@").casefold()
    if normalized_username and _mentions_username(
        message,
        text=text,
        normalized_username=normalized_username,
    ):
        return True
    return bool(_DERP_NAME.search(text))


def _mentions_username(
    message: Message,
    *,
    text: str,
    normalized_username: str,
) -> bool:
    entities = (
        message.entities if message.text is not None else message.caption_entities
    )
    for entity in entities or ():
        if entity.type == "mention" and entity.extract_from(text).casefold() == (
            f"@{normalized_username}"
        ):
            return True

    # Real Telegram updates carry mention entities. The strict fallback keeps
    # synthetic/test messages useful without accepting username prefixes.
    username = re.escape(normalized_username)
    return bool(
        re.search(
            rf"(?<!\w)@{username}(?!\w)",
            text,
            re.IGNORECASE,
        )
    )


__all__ = [
    "CONTEXT_NOTICE_VERSION",
    "capture_kind_for_message",
    "is_explicit_invocation",
]
