"""Outbound messages: Rich Messages first, plain text as the fallback.

LLM answers and system copy are Markdown and go out via ``sendRichMessage``.
A rejected rich message (BadRequest means nothing was delivered) is resent as
plain text, split to Telegram's limits. Untrusted (model-written) Markdown is
stripped of interactive tags so a prompt-injected answer cannot render
callback buttons that look like Derp's own controls, and of remote media and
link previews, which Telegram's servers would fetch — an injected answer could
otherwise leak chat content in a URL. Rich messages come back from Telegram as
blocks without text, so the sent Markdown is kept as their ``text`` for history.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    EphemeralMessageParameters,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    InputRichMessage,
    LinkPreviewOptions,
    Message,
    ReplyParameters,
)

from derp.telegram.text import PLAIN_LIMIT, RICH_LIMIT, hard_breaks, split_text

_INTERACTIVE_TAGS = re.compile(r"</?\s*tg-(?:button|button-row|thinking)\b[^>]*>", re.IGNORECASE)
_MEDIA_TAGS = re.compile(
    r"<\s*/?\s*(?:img|video|audio|source|picture|iframe|embed|object)\b[^>]*>", re.IGNORECASE
)
_REMOTE_MEDIA = re.compile(
    r"!\[([^\]]*)\](?=\[|\((?!\s*<?tg:))"
)  # ![alt](url) / ![alt][ref], not tg:// links
_FENCE = "```"
RICH_CHUNK = RICH_LIMIT - 768  # headroom for fence repair and entity overhead


def rich(markdown: str) -> InputRichMessage:
    """Rich Markdown as written: line breaks stay line breaks (Telegram would join them into one line)."""
    return InputRichMessage(markdown=hard_breaks(markdown))


@dataclass(frozen=True, slots=True)
class Target:
    """Where a message goes: chat, forum topic, and the message it replies to (same chat).

    A reply to an ephemeral command (message id 0) is itself ephemeral: only the sender sees it,
    and Telegram accepts it only within 15 seconds and may not deliver it.
    """

    chat_id: int
    thread_id: int | None = None
    reply_to: int | None = None
    ephemeral_to: int | None = None  # the user who alone sees the message
    ephemeral_reply_to: int | None = None  # their ephemeral message being answered

    @classmethod
    def reply_to_message(cls, message: Message) -> Target:
        thread = message.message_thread_id if message.is_topic_message else None
        if message.ephemeral_message_id and message.from_user is not None:
            return cls(message.chat.id, thread, None, message.from_user.id, message.ephemeral_message_id)
        return cls(message.chat.id, thread, message.message_id or None)

    def replying(self, message_id: int | None) -> Target:
        # Ephemeral chunks all answer the same command: non-admin bots may only send them as replies.
        return Target(self.chat_id, self.thread_id, message_id, self.ephemeral_to, self.ephemeral_reply_to)

    @property
    def reply_parameters(self) -> ReplyParameters | None:
        if self.ephemeral_reply_to is not None:
            return ReplyParameters(ephemeral_message_id=self.ephemeral_reply_to)
        if self.reply_to is None:
            return None
        return ReplyParameters(message_id=self.reply_to, allow_sending_without_reply=True)

    @property
    def ephemeral(self) -> EphemeralMessageParameters | None:
        return EphemeralMessageParameters(receiver_user_id=self.ephemeral_to) if self.ephemeral_to else None


def _strip_once(markdown: str) -> str:
    markdown = _MEDIA_TAGS.sub("", _INTERACTIVE_TAGS.sub("", markdown))
    return _REMOTE_MEDIA.sub(lambda m: f"[{m.group(1) or 'link'}]", markdown)


def sanitize_untrusted(markdown: str) -> str:
    # Repeat until stable: removing an inner tag must not assemble an outer one.
    while (cleaned := _strip_once(markdown)) != markdown:
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


class PartialDeliveryError(TelegramAPIError):
    """A multi-part send failed after Telegram had accepted the first parts (``sent``)."""

    def __init__(self, error: TelegramAPIError, sent: list[Message]) -> None:
        super().__init__(error.method, error.message)
        self.sent = sent


async def send_markdown(
    bot: Bot,
    target: Target,
    markdown: str,
    *,
    trusted: bool = True,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> list[Message]:
    """Send Markdown as one or more rich messages; the keyboard goes on the last one.

    A failure after some parts went out raises ``PartialDeliveryError`` carrying them.
    """
    if not trusted:
        markdown = sanitize_untrusted(markdown)
    chunks = split_rich(markdown) if markdown.strip() else ["…"]
    sent: list[Message] = []
    try:
        await _send_chunks(bot, target, chunks, sent, trusted=trusted, reply_markup=reply_markup)
    except TelegramAPIError as error:
        if sent:
            raise PartialDeliveryError(error, sent) from error
        raise
    return sent


async def send_privately(
    bot: Bot, user_id: int, markdown: str, *, reply_markup: InlineKeyboardMarkup | None = None
) -> bool:
    """DM someone; False when Telegram won't let Derp start the conversation (never pressed Start)."""
    try:
        await send_markdown(bot, Target(user_id), markdown, reply_markup=reply_markup)
    except TelegramForbiddenError, TelegramBadRequest:
        return False
    return True


async def _send_chunks(
    bot: Bot,
    target: Target,
    chunks: list[str],
    sent: list[Message],
    *,
    trusted: bool,
    reply_markup: InlineKeyboardMarkup | None,
) -> None:
    for index, chunk in enumerate(chunks):
        last = index == len(chunks) - 1
        target_for_chunk = target if index == 0 else target.replying(None)
        try:
            message = await bot.send_rich_message(
                chat_id=target.chat_id,
                message_thread_id=target.thread_id,
                rich_message=rich(chunk),
                reply_parameters=target_for_chunk.reply_parameters,
                reply_markup=reply_markup if last else None,
                ephemeral_message_parameters=target.ephemeral,
            )
        except TelegramBadRequest:
            sent.extend(
                await send_plain(
                    bot,
                    target_for_chunk,
                    chunk,
                    reply_markup=reply_markup if last else None,
                    previews=trusted,
                )
            )
        else:
            sent.append(message if message.text else message.model_copy(update={"text": chunk}))


async def send_plain(
    bot: Bot,
    target: Target,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    previews: bool = True,
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
                link_preview_options=None if previews else LinkPreviewOptions(is_disabled=True),
                reply_parameters=(target if index == 0 else target.replying(None)).reply_parameters,
                ephemeral_message_parameters=target.ephemeral,
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
