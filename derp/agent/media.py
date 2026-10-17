"""Images a run looks at: the current message's (album included) and the replied-to message's.

Vision-capable pins get the pixels; every inspected image also gets a one-line
description stored on ``message_media`` (and folded into search text), so later
runs — and models without vision — see "[photo: a grey cat on a laptop]"
instead of a bare placeholder. Descriptions are written only for images a run
actually inspected. Notes for one run are made concurrently under a timeout;
every provider response is ledgered, and an image whose paid note came back
empty is marked with an empty description so it is never paid for twice.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic_ai import Agent, BinaryContent, capture_run_messages
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models import Model
from sqlalchemy import false, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.db.engine import Database
from derp.history.models import Message, MessageMedia
from derp.inference.ledger import record_usage
from derp.inference.openrouter import chat_settings, response_usage
from derp.inference.roles import RolePin
from derp.telegram.media import Media, MediaGateway, MediaUnavailableError

log = logging.getLogger("derp.media")
IMAGE_LIKE = ("photo", "sticker", "document", "animation")
MAX_IMAGES = 4  # also bounds the concurrent note calls of one run
NOTE_TOKENS = 300  # room for a reasoning model's mandatory thinking before the one line
NOTE_SECONDS = 30
DESCRIBE_PROMPT = (
    "Describe this image in one line (at most 25 words) for a chat log. "
    "Mention any visible text verbatim. No preamble."
)


@dataclass(slots=True)
class Inspected:
    row: MessageMedia
    image: Media


async def referenced_media(
    session: AsyncSession, chat_id: int, message_id: int, reply_to: int | None
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
            MessageMedia.type.in_(IMAGE_LIKE),
        )
        .order_by(MessageMedia.message_id == message_id, MessageMedia.message_id, MessageMedia.idx)
    )
    return list(rows)[-MAX_IMAGES:]


async def inspect(gateway: MediaGateway, rows: Sequence[MessageMedia]) -> list[Inspected]:
    """Download what can be downloaded; animated stickers, videos and oversized files are skipped."""
    inspected: list[Inspected] = []
    for row in rows:
        try:
            image = await gateway.image(
                row.file_id, bot_id=row.bot_id, key=row.file_unique_id, size_hint=row.file_size
            )
        except MediaUnavailableError as unavailable:
            log.info("media #%s skipped: %s", row.message_id, unavailable.reason)
            continue
        inspected.append(Inspected(row, image))
    return inspected


def as_content(items: Sequence[Inspected]) -> list[str | BinaryContent]:
    content: list[str | BinaryContent] = []
    for item in items:
        content.append(f"(image from #{item.row.message_id})")
        content.append(BinaryContent(item.image.data, media_type=item.image.media_type))
    return content


async def describe(model: Model, pin: RolePin, image: Media) -> tuple[str, list[ModelResponse]]:
    """A one-line note ("" when the model gave none) and every response it was paid for, failures included."""
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
    return text, [m for m in captured if isinstance(m, ModelResponse)]


async def store_description(session: AsyncSession, media: MessageMedia, description: str) -> None:
    row = await session.get(MessageMedia, (media.chat_id, media.message_id, media.idx))
    message = await session.get(Message, (media.chat_id, media.message_id))
    if row is None or message is None:
        return
    row.description = description
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
    for item, (text, paid) in zip(pending, notes, strict=True):
        if not text and not paid:  # nothing came back, nothing was paid: a later run may try again
            continue
        async with db.transaction() as session:
            for response in paid:
                record_usage(
                    session,
                    response_usage(response),
                    kind="media",
                    funding=funding,
                    run_id=run_id,
                    chat_id=chat_id,
                    user_id=user_id,
                )
            await store_description(session, item.row, text)
        item.row.description = text
