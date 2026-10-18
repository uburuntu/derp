"""Images a run looks at: the current message's (album included) and the replied-to message's.

When neither has one, two follow-ups still bring a photo along (``run_media``):
the asker's own photo (or album) sent just before the question in the same topic,
and, for a reply to one of Derp's answers, the image the run behind that answer
looked at (one hop: its trigger's or the message that trigger replied to).

Vision-capable pins get the pixels; every inspected image also gets a one-line
description stored on ``message_media`` (and folded into search text), so later
runs — and models without vision — see "[photo: a grey cat on a laptop]"
instead of a bare placeholder. Descriptions are written only for images a run
actually inspected. Asked for explicitly (``frames``), videos, GIFs and video
messages are seen through one still frame. Notes for one run are made concurrently
under a timeout; every provider response is ledgered, and an image whose paid note
came back empty is marked with an empty description so it is never paid for twice.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta

from pydantic_ai import Agent, BinaryContent, capture_run_messages
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model
from sqlalchemy import false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.agent.models import AgentRun, RunOutput
from derp.agent.runs import ledger_side_call
from derp.db.engine import Database
from derp.history.models import Message, MessageMedia
from derp.history.render import kind_label
from derp.inference.openrouter import chat_settings
from derp.inference.roles import ModelRole, RolePin
from derp.telegram.media import Media, MediaGateway, MediaUnavailableError
from derp.tools.effects import still
from derp.tools.workers import EffectError, WorkerPool

log = logging.getLogger("derp.media")
IMAGE_LIKE = ("photo", "sticker", "document", "animation")
CLIPS = ("animation", "video", "video_note")
MAX_IMAGES = 4  # also bounds the concurrent note calls of one run
NOTE_TOKENS = 300  # room for a reasoning model's mandatory thinking before the one line
NOTE_SECONDS = 30
FOLLOW_UP_SECONDS = 60  # a photo the asker sent this shortly before the question belongs to it
NOTE_INPUT_TOKENS = 1_500  # prompt plus one image, for pricing a note that came back without a cost
DESCRIBE_PROMPT = (
    "Describe this image in one line (at most 25 words) for a chat log. "
    "Mention any visible text verbatim. No preamble."
)


@dataclass(slots=True)
class Inspected:
    row: MessageMedia
    image: Media
    still: str = ""  # how a picture stands in for a clip: "frame" (taken from it)


async def referenced_media(
    session: AsyncSession,
    chat_id: int,
    message_id: int,
    reply_to: int | None,
    types: Sequence[str] = IMAGE_LIKE,
) -> list[MessageMedia]:
    """Image-like media of the message (and its album) and of the message it replies to."""
    anchors = [message_id, *([reply_to] if reply_to else [])]
    groups = [
        g
        for g in await session.scalars(
            select(Message.media_group_id).where(
                Message.chat_id == chat_id,
                Message.message_id.in_(anchors),
                Message.media_group_id.is_not(None),
            )
        )
        if g
    ]
    ids = select(Message.message_id).where(
        Message.chat_id == chat_id,
        Message.deleted_at.is_(None),
        or_(Message.message_id.in_(anchors), Message.media_group_id.in_(groups) if groups else false()),
    )
    rows = await session.scalars(
        select(MessageMedia)
        .where(
            MessageMedia.chat_id == chat_id,
            MessageMedia.message_id.in_(ids),
            MessageMedia.type.in_(types),
        )
        .order_by(MessageMedia.message_id == message_id, MessageMedia.message_id, MessageMedia.idx)
    )
    return list(rows)[-MAX_IMAGES:]


@dataclass(slots=True)
class Referenced:
    rows: list[MessageMedia]
    # Messages whose images are shown but not described by this run (see ``run_media``).
    unnoted: frozenset[int] = field(default_factory=frozenset[int])


async def preceding_media(session: AsyncSession, trigger: Message) -> list[MessageMedia]:
    """The asker's previous stored message in the topic, if it came within ``FOLLOW_UP_SECONDS``: its images.

    Only that one message is looked at (its album included): an older one, or one with another message
    from the same person in between, is not part of this question.
    """
    if trigger.user_id is None or trigger.sender_chat_id is not None or trigger.is_derp:
        return []  # anonymous admins and channels share a sender: "the same person" isn't known
    previous = await session.scalar(
        select(Message)
        .where(
            Message.chat_id == trigger.chat_id,
            Message.user_id == trigger.user_id,
            Message.message_id < trigger.message_id,
            Message.date >= trigger.date - timedelta(seconds=FOLLOW_UP_SECONDS),
            Message.thread_id == trigger.thread_id
            if trigger.thread_id is not None
            else Message.thread_id.is_(None),
        )
        .order_by(Message.message_id.desc())
        .limit(1)
    )
    if previous is None or previous.deleted_at is not None or previous.sender_chat_id is not None:
        return []
    return await referenced_media(session, trigger.chat_id, previous.message_id, None)


async def answered_media(session: AsyncSession, chat_id: int, output_id: int) -> list[MessageMedia]:
    """When ``output_id`` is one of Derp's answers: the images its run looked at, one hop back, else nothing."""
    trigger_id = await session.scalar(
        select(AgentRun.trigger_message_id)
        .join(RunOutput, RunOutput.run_id == AgentRun.id)
        .where(
            RunOutput.chat_id == chat_id,
            RunOutput.message_id == output_id,
            RunOutput.kind != "reaction",  # a reaction's row names the message reacted to
            AgentRun.chat_id == chat_id,
        )
        .limit(1)
    )
    earlier = await session.get(Message, (chat_id, trigger_id)) if trigger_id else None
    if earlier is None or earlier.deleted_at is not None:
        return []
    rows = await referenced_media(session, chat_id, earlier.message_id, earlier.reply_to_message_id)
    return rows or await preceding_media(session, earlier)


async def run_media(session: AsyncSession, chat_id: int, message_id: int, reply_to: int | None) -> Referenced:
    """What a run looks at: ``referenced_media``, else the asker's photo just before and the answered run's image.

    Prompt cache: the pixels go only into the run's final request, but a description stored for an image
    rewrites that message's transcript line in later windows. The asker's photo just before the question is
    at the very end of this run's window, so describing it costs the cache a line or two; the image behind an
    answered run sits further back and is never described here (``unnoted``), so older lines stay
    byte-stable.
    """
    own = await referenced_media(session, chat_id, message_id, reply_to)
    trigger = await session.get(Message, (chat_id, message_id)) if message_id else None
    if own or trigger is None:
        return Referenced(own)
    answered = await answered_media(session, chat_id, reply_to) if reply_to else []
    recent = await preceding_media(session, trigger)
    shown = {(r.message_id, r.idx) for r in recent}
    rows = [*(r for r in answered if (r.message_id, r.idx) not in shown), *recent][-MAX_IMAGES:]
    return Referenced(rows, frozenset(r.message_id for r in answered) - {r.message_id for r in recent})


async def inspect(
    gateway: MediaGateway, rows: Sequence[MessageMedia], *, frames: WorkerPool | None = None
) -> list[Inspected]:
    """Each row as a picture a model can take (``MediaGateway.image``), or skipped when it has none.

    The row's own file when it is a picture; else, with ``frames`` (the worker pool of an explicit look),
    a still frame of the clip.
    """
    inspected: list[Inspected] = []
    for row in rows:
        try:
            inspected.append(await _picture(gateway, row, frames))
        except MediaUnavailableError as unavailable:
            log.info("media #%s skipped: %s", row.message_id, unavailable.reason)
    return inspected


async def _picture(gateway: MediaGateway, row: MessageMedia, frames: WorkerPool | None) -> Inspected:
    try:
        image = await gateway.image(
            row.file_id,
            bot_id=row.bot_id,
            key=row.file_unique_id,
            size_hint=row.file_size,
            mime_type=row.mime_type,
        )
        return Inspected(row, image)
    except MediaUnavailableError:
        if frames is None:
            raise
    try:
        clip = await gateway.fetch(
            row.file_id, bot_id=row.bot_id, key=row.file_unique_id, size_hint=row.file_size
        )
        return Inspected(row, await gateway.normalize(await still(frames, clip)), still="frame")
    except EffectError as failed:
        raise MediaUnavailableError(f"no frame ({failed.reason})") from None


def as_content(items: Sequence[Inspected]) -> list[str | BinaryContent]:
    content: list[str | BinaryContent] = []
    for item in items:
        what = f"a still frame of the {kind_label(item.row.type)}" if item.still else "image"
        content.append(f"({what} from #{item.row.message_id})")
        content.append(BinaryContent(item.image.data, media_type=item.image.media_type))
    return content


async def describe(model: Model, pin: RolePin, image: Media) -> tuple[str, list[ModelMessage]]:
    """A one-line note ("" when the model gave none) and the call's messages, failures included."""
    agent = Agent(model, model_settings=chat_settings(pin, max_tokens=NOTE_TOKENS))
    text = ""
    with capture_run_messages() as captured:
        try:
            async with asyncio.timeout(NOTE_SECONDS):
                result = await agent.run(
                    [DESCRIBE_PROMPT, BinaryContent(image.data, media_type=image.media_type)]
                )
            text = " ".join(result.output.split())[:300]
        except Exception as error:  # noqa: BLE001 - a missing note never fails the run
            log.info("media note failed: %s", type(error).__name__)
    return text, list(captured)


async def store_description(
    session: AsyncSession, media: MessageMedia, description: str, run_id: int
) -> None:
    row = await session.get(MessageMedia, (media.chat_id, media.message_id, media.idx))
    message = await session.get(Message, (media.chat_id, media.message_id))
    if row is None or message is None:
        return
    row.description, row.description_run_id = description, run_id
    notes = [
        m.transcript or m.description or ""
        for m in await session.scalars(
            select(MessageMedia).where(
                MessageMedia.chat_id == media.chat_id, MessageMedia.message_id == media.message_id
            )
        )
    ]
    message.extra_text = " ".join(n for n in notes if n) or None


async def note_images(
    db: Database,
    model: Model,
    pin: RolePin,
    items: Sequence[Inspected],
    *,
    run_id: int,
    funding: str,
    role: ModelRole,
    chat_id: int,
    user_id: int,
) -> None:
    """Describe inspected images never noted before, concurrently; each call is ledgered against the run.

    An empty description marks an image whose paid note came back empty: it renders as a bare
    placeholder and is not described (and paid for) again.
    """
    if not pin.vision:
        return
    pending = [item for item in items if item.row.description is None]
    notes = await asyncio.gather(*(describe(model, pin, item.image) for item in pending))
    for item, (text, messages) in zip(pending, notes, strict=True):
        paid = await ledger_side_call(
            db, messages, kind="media", funding=funding, role=role, context_tokens=NOTE_INPUT_TOKENS,
            run_id=run_id, chat_id=chat_id, user_id=user_id,
        )  # fmt: skip
        if not text and not paid:  # nothing came back, nothing was paid: a later run may try again
            continue
        async with db.transaction() as session:
            await store_description(session, item.row, text, run_id)
        item.row.description = text
