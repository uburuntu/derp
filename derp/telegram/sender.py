"""Outbound messages: Rich Messages first, plain text as the fallback.

LLM answers and system copy are Markdown and go out via ``sendRichMessage``.
A rejected rich message (BadRequest means nothing was delivered) is resent as
plain text, split to Telegram's limits. Untrusted (model-written) Markdown is
stripped of interactive tags so a prompt-injected answer cannot render
callback buttons that look like Derp's own controls.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    InputRichMessage,
    Message,
    ReplyParameters,
)

from derp.telegram.text import PLAIN_LIMIT, RICH_LIMIT, split_text

_INTERACTIVE_TAGS = re.compile(r"</?\s*tg-(?:button|button-row|thinking)\b[^>]*>", re.IGNORECASE)
_FENCE = "```"
RICH_CHUNK = RICH_LIMIT - 768  # headroom for fence repair and entity overhead


@dataclass(frozen=True, slots=True)
class Target:
    """Where a message goes: chat, forum topic, and the message it replies to (same chat)."""

    chat_id: int
    thread_id: int | None = None
    reply_to: int | None = None

    @classmethod
    def reply_to_message(cls, message: Message) -> Target:
        thread = message.message_thread_id if message.is_topic_message else None
        return cls(message.chat.id, thread, message.message_id or None)

    def replying(self, message_id: int | None) -> Target:
        return Target(self.chat_id, self.thread_id, message_id)

    @property
    def reply_parameters(self) -> ReplyParameters | None:
        if self.reply_to is None:
            return None
        return ReplyParameters(message_id=self.reply_to, allow_sending_without_reply=True)


def sanitize_untrusted(markdown: str) -> str:
    # Repeat until stable: removing an inner tag must not assemble an outer one.
    while (cleaned := _INTERACTIVE_TAGS.sub("", markdown)) != markdown:
        markdown = cleaned
    return markdown


def split_rich(markdown: str, limit: int = RICH_CHUNK) -> list[str]:
    """Split long Markdown, closing and reopening code fences across chunk boundaries."""
    chunks = split_text(markdown, limit, measure=len)
    repaired: list[str] = []
    reopen = ""
    for chunk in chunks:
        chunk = reopen + chunk
        reopen = ""
        if chunk.count(_FENCE) % 2:
            language = chunk[chunk.rfind(_FENCE) + 3 :].split("\n", 1)[0].strip()
            chunk += "\n" + _FENCE
            reopen = f"{_FENCE}{language}\n"
        repaired.append(chunk)
    return repaired


async def send_markdown(
    bot: Bot,
    target: Target,
    markdown: str,
    *,
    trusted: bool = True,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> list[Message]:
    """Send Markdown as one or more rich messages; the keyboard goes on the last one."""
    if not trusted:
        markdown = sanitize_untrusted(markdown)
    chunks = split_rich(markdown) if markdown.strip() else ["…"]
    sent: list[Message] = []
    for index, chunk in enumerate(chunks):
        last = index == len(chunks) - 1
        target_for_chunk = target if index == 0 else target.replying(None)
        try:
            sent.append(
                await bot.send_rich_message(
                    chat_id=target.chat_id,
                    message_thread_id=target.thread_id,
                    rich_message=InputRichMessage(markdown=chunk),
                    reply_parameters=target_for_chunk.reply_parameters,
                    reply_markup=reply_markup if last else None,
                )
            )
        except TelegramBadRequest:
            sent.extend(
                await send_plain(bot, target_for_chunk, chunk, reply_markup=reply_markup if last else None)
            )
    return sent


async def send_plain(
    bot: Bot, target: Target, text: str, *, reply_markup: InlineKeyboardMarkup | None = None
) -> list[Message]:
    chunks = split_text(text, PLAIN_LIMIT) if text.strip() else ["…"]
    sent: list[Message] = []
    for index, chunk in enumerate(chunks):
        last = index == len(chunks) - 1
        sent.append(
            await bot.send_message(
                chat_id=target.chat_id,
                message_thread_id=target.thread_id,
                text=chunk,
                reply_parameters=(target if index == 0 else target.replying(None)).reply_parameters,
                reply_markup=reply_markup if last else None,
            )
        )
    return sent


async def send_album(
    bot: Bot, target: Target, media: Sequence[InputMediaPhoto | InputMediaVideo]
) -> list[Message]:
    """Photos/videos as albums of 2–10 (balanced batches); a lone item is sent on its own."""
    if not media:
        return []
    if len(media) == 1:
        return [await _send_single(bot, target, media[0])]
    batches = -(-len(media) // 10)
    size = -(-len(media) // batches)
    sent: list[Message] = []
    for start in range(0, len(media), size):
        sent.extend(
            await bot.send_media_group(
                chat_id=target.chat_id,
                message_thread_id=target.thread_id,
                media=list(media[start : start + size]),
                reply_parameters=target.reply_parameters if start == 0 else None,
            )
        )
    return sent


async def _send_single(bot: Bot, target: Target, item: InputMediaPhoto | InputMediaVideo) -> Message:
    common = {
        "chat_id": target.chat_id,
        "message_thread_id": target.thread_id,
        "caption": item.caption,
        "reply_parameters": target.reply_parameters,
    }
    if isinstance(item, InputMediaPhoto):
        return await bot.send_photo(photo=item.media, **common)
    return await bot.send_video(video=item.media, **common)
