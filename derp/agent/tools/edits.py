"""Edit an ordinary Derp answer, preserving its Telegram identity and the cost of any generated replacement."""

from asyncio import Lock
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from weakref import WeakValueDictionary

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    BufferedInputFile,
    InputFile,
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaLivePhoto,
    InputMediaPhoto,
    InputMediaUnion,
    InputMediaVideo,
)
from aiogram.types import Message as TgMessage
from sqlalchemy import select

from derp.agent.deps import RunDeps
from derp.agent.models import RunOutput
from derp.agent.outputs import (
    Kept,
    Refused,
    Unconfirmed,
    check_budget,
    deliver,
    output_kind,
    record,
    send_file,
    settle,
    unconfirmed,
)
from derp.agent.tools.sources import MESSAGE, kept_ref
from derp.history.links import message_links
from derp.history.models import Message, MessageMedia
from derp.locales import t
from derp.telegram.policy import AMBIGUOUS
from derp.telegram.sender import Target, rich, sanitize_untrusted, send_markdown

EDITABLE_MEDIA = {"photo", "live_photo", "video", "animation", "audio", "document"}
CAPTIONED = EDITABLE_MEDIA | {"voice"}
ALBUM_VISUAL = {"photo", "live_photo", "video"}
CAPTION_LIMIT = 1024
# Runs from different topics in the same chat can target the same answer concurrently.
_edit_locks: WeakValueDictionary[tuple[int, int], Lock] = WeakValueDictionary()


@dataclass(frozen=True, slots=True)
class Replacement:
    kind: str
    file: str | InputFile
    cover: str | None = None

    def input(self, caption: str | None) -> InputMediaUnion:
        match self.kind:
            case "photo":
                return InputMediaPhoto(media=self.file, caption=caption)
            case "video":
                return InputMediaVideo(media=self.file, caption=caption)
            case "audio":
                return InputMediaAudio(media=self.file, caption=caption)
            case "document":
                return InputMediaDocument(media=self.file, caption=caption)
            case "animation":
                return InputMediaAnimation(media=self.file, caption=caption)
            case "live_photo" if self.cover is not None and isinstance(self.file, str):
                return InputMediaLivePhoto(media=self.file, photo=self.cover, caption=caption)
            case _:
                raise Refused("This file can't be used in an in-place media edit.")


async def _replacement(deps: RunDeps, source: str, kept: Kept | None) -> Replacement:
    if kept is not None:
        made = kept.made
        return Replacement(made.sendable, BufferedInputFile(made.data, filename=made.filename))
    ref = MESSAGE.fullmatch(source.strip())
    if ref is None:
        raise Refused('Name a stored file as "#123" or an unused result from this answer as "r1".')
    async with deps.db.session() as session:
        found = await session.execute(
            select(MessageMedia, Message.raw)
            .join(
                Message,
                (Message.chat_id == MessageMedia.chat_id) & (Message.message_id == MessageMedia.message_id),
            )
            .where(
                Message.chat_id == deps.chat.id,
                Message.message_id == int(ref[1]),
                Message.deleted_at.is_(None),
                MessageMedia.bot_id == deps.bot.id,
            )
            .order_by(MessageMedia.idx)
            .limit(1)
        )
        row = found.first()
    if row is None:
        raise Refused("No usable file from that message is available to this bot.")
    file, raw = row
    # Before live photos had their own kind, capture stored the ordinary still and the raw video.
    if (
        file.type == "photo"
        and raw
        and (clip := raw.get("live_photo"))
        and isinstance(clip.get("file_id"), str)
    ):
        return Replacement("live_photo", clip["file_id"], file.file_id)
    if file.type == "live_photo" and file.thumb_file_id is None:
        raise Refused("The static photo needed to resend this live photo is missing.")
    return Replacement(file.type, file.file_id, file.thumb_file_id)


async def _ordinary(deps: RunDeps, message_id: int) -> Message:
    async with deps.db.session() as session:
        row = await session.get(Message, (deps.chat.id, message_id))
        if (
            row is None
            or not row.is_derp
            or row.deleted_at is not None
            or row.user_id not in (None, deps.bot.id)
        ):
            raise Refused("I can edit only my own messages still stored in this chat.")
        if row.kind == "invoice" or row.payload.get("has_controls"):
            raise Refused("Payment and control cards have their own controls.")
        if row.kind == "text" and await session.scalar(
            select(RunOutput.id)
            .where(
                RunOutput.chat_id == row.chat_id,
                RunOutput.message_id == message_id,
                RunOutput.kind == "other",
            )
            .limit(1)
        ):
            # Older purchase menus were recorded as 'other' text without their buttons in history.
            raise Refused("This is a control card, not an ordinary answer.")
    if row.kind == "other" and row.raw and "live_photo" in row.raw:
        row.kind = "live_photo"  # detached legacy row; its next confirmed edit normalizes it
    return row


def _compatible(row: Message, replacement: Replacement) -> bool:
    if replacement.kind not in EDITABLE_MEDIA or row.kind not in EDITABLE_MEDIA | {"text"}:
        return False
    if row.media_group_id:
        return replacement.kind in ALBUM_VISUAL if row.kind in ALBUM_VISUAL else replacement.kind == row.kind
    return True


async def _revise(
    deps: RunDeps, row: Message, text: str | None, caption: str | None, replacement: Replacement | None
) -> TgMessage | None:
    """None means this content needs a fresh message. Only an accepted edit returns Telegram's message."""
    chat_id, message_id = row.chat_id, row.message_id
    if text is not None:
        if row.kind != "text":
            return None
        async with deps.db.session() as session:
            links = await message_links(session, deps.chat, text)
        result = await deps.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, rich_message=rich(text, links)
        )
        if isinstance(result, TgMessage):
            result = result.model_copy(update={"text": text})
    elif replacement is not None:
        if not _compatible(row, replacement):
            return None
        result = await deps.bot.edit_message_media(
            chat_id=chat_id, message_id=message_id, media=replacement.input(caption)
        )
    else:
        if row.kind not in CAPTIONED:
            return None
        result = await deps.bot.edit_message_caption(
            chat_id=chat_id, message_id=message_id, caption=caption or ""
        )
    if not isinstance(
        result, TgMessage
    ):  # Only an inline edit can return True; our target has chat and message ids.
        raise Unconfirmed(unconfirmed("edit"))
    return result if result.edit_date else result.model_copy(update={"edit_date": datetime.now(UTC)})


async def _fallback(
    deps: RunDeps, target: Target, replacement: Replacement | None, body: str, kept: Kept | None
) -> str:
    """Send a linked replacement after a definite refusal; never retry an uncertain edit or send."""
    note = t(deps.locale, "agent.edit_replacement")
    explanation = f"{body}\n\n{note}" if body else note
    if replacement is None:
        async with deps.db.session() as session:
            links = await message_links(session, deps.chat, explanation)
        sent = await deliver(
            deps,
            "message",
            "replacement",
            lambda: send_markdown(deps.bot, target, explanation, trusted=False, links=links),
            budget=False,
        )
    else:
        captioned = replacement.kind in CAPTIONED
        fits = captioned and len(explanation) <= CAPTION_LIMIT
        sent = await deliver(
            deps,
            output_kind(replacement.kind),
            "replacement",
            lambda: send_file(
                deps.bot,
                target,
                replacement.kind,
                replacement.file,
                caption=explanation if fits else body if captioned else None,
                cover=replacement.cover,
            ),
            budget=False,
            spoken=kept.made.spoken if kept else None,
        )
        if not fits:
            # The replacement is already delivered even if this explanatory note fails.
            with suppress(Refused):
                await deliver(
                    deps,
                    "message",
                    "replacement note",
                    lambda: send_markdown(
                        deps.bot, target, note if captioned else explanation, trusted=False
                    ),
                    rides=True,
                )
    return f"Posted the replacement as #{sent[0].message_id}, replying to #{target.reply_to}. End with a dot if done."


async def edit_message(
    deps: RunDeps, message_id: int, *, text: str | None, caption: str | None, media: str | None
) -> str:
    async with _edit_locks.setdefault((deps.chat.id, message_id), Lock()):
        return await _edit(deps, message_id, text, caption, media)


async def _edit(
    deps: RunDeps, message_id: int, text: str | None, caption: str | None, media: str | None
) -> str:
    ref = kept_ref(media)
    kept = deps.kept.get(ref) if ref else None
    if kept and kept.used:
        return "That kept result was already used."
    accepted = False
    try:
        check_budget(deps, using=ref)
        if kept:
            kept.used = (
                True  # this media belongs to the edit, even on failure: flush must never post it again
            )
        if message_id in deps.uncertain_edits:
            raise Unconfirmed(unconfirmed("edit"))
        if text is not None and (not text.strip() or media is not None or caption is not None):
            raise Refused("Use text for a full text replacement, or media and/or caption.")
        if media is not None and not media.strip():
            raise Refused("Name a stored file or a kept result for media.")
        if text is None and caption is None and media is None:
            raise Refused("Give the replacement text, caption or media.")
        if caption is not None and len(caption) > CAPTION_LIMIT:
            raise Refused("A caption can contain up to 1024 characters.")
        row = await _ordinary(deps, message_id)
        target = Target(row.chat_id, row.thread_id, message_id, require_reply=True)
        replacement = await _replacement(deps, media, kept) if media else None
        if text is not None:
            text = sanitize_untrusted(text)
            if not text.strip():
                raise Refused("The new text has no visible content.")
        if replacement and caption is None and row.kind in CAPTIONED:
            caption = row.text
        try:
            edited = await _revise(deps, row, text, caption, replacement)
        except TelegramBadRequest as error:
            if (
                "message is not modified" in error.message.lower()
                or "message_not_modified" in error.message.lower()
            ):
                deps.edited_target = target  # acknowledgement is optional, even when no edit was needed
                return f"#{message_id} already has this content. End with a dot if done."
            edited = None
        if edited is None:
            result = await _fallback(deps, target, replacement, text or caption or "", kept)
        else:
            accepted = True
            await record(deps, [edited], "edit", spoken=kept.made.spoken if kept else None)
            result = f"Edited #{message_id} in place. End with a dot to stay quiet or acknowledge the update."
        accepted = True
        deps.edited_target = target
        return result
    except (*AMBIGUOUS, Unconfirmed):
        deps.uncertain_edits.add(message_id)
        return unconfirmed("edit or replacement")
    except TelegramAPIError as error:
        return f"Telegram refused the edit ({type(error).__name__})."
    except Refused as refused:
        return str(refused)
    finally:
        if kept and kept.used:
            await settle(deps, kept.made.pending, delivered=accepted)
