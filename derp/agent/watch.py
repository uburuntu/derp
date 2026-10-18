"""Whole-file looks for ``inspect_media``: clips with their sound, audio and PDFs, on the media pin.

The chat model never takes these files itself: the media pin (``ModelRole.MEDIA``, ZDR routes only) gets
the file plus the question and its notes become the tool result — timestamps for clips and audio, page
numbers for PDFs, speech quoted. A look is for paid and sponsored answers within their tier's limits
(``settings.watching``) and the Bot API's download cap; it is priced before it is made and refused when
the run's hold can't cover it and one more turn. Whatever isn't watched says why (``Look.why``), so the
chat model doesn't guess, and falls back to a still frame or preview picture (``media.inspect``).

Every response is ledgered against the run as ``kind='media'``; the notes' first line, a one-line
description, is stored on the media row like an image note (``media.store_description``), so later
windows and search see it; the notes themselves stay in the tool result.
"""

import asyncio
import logging
import re
import zlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from pydantic_ai import Agent, BinaryContent, RunContext, capture_run_messages

from derp.agent.deps import RunDeps
from derp.agent.media import store_description
from derp.agent.outputs import hold_left
from derp.agent.runs import ledger_side_call
from derp.config import WatchLimits
from derp.history.models import MessageMedia
from derp.history.render import duration_label, kind_label
from derp.inference.ledger import estimate_cost
from derp.inference.models import ModelPin
from derp.inference.openrouter import AUDIO_FORMATS, chat_settings
from derp.inference.roles import ModelRole
from derp.money.funding import Funding
from derp.telegram.media import BOT_API_DOWNLOAD_LIMIT, MediaUnavailableError, TooLargeError, known_type

log = logging.getLogger("derp.watch")
type Kind = Literal["clip", "audio", "pdf"]
# Video formats ``video_url`` takes (OpenRouter's video input guide), by MIME type.
VIDEO_TYPES = {
    "video/mp4": "video/mp4",
    "video/mpeg": "video/mpeg",
    "video/webm": "video/webm",
    "video/quicktime": "video/mov",
}
STICKER_SECONDS = 3  # Telegram's longest video sticker; their rows carry no duration
VERBS: dict[Kind, str] = {"clip": "watched", "audio": "listened to", "pdf": "read"}
SOUNDTRACKS = ("video", "video_note")
INSTRUCTIONS = (
    "You are the eyes and ears of a chat bot that can't open this file itself; it answers the chat from your "
    "notes. Report only what is really there, specifically, and say when something is unclear. No preamble.\n"
    "First line: the whole file in one line (at most 25 words) for the chat log.\n"
    "Then a blank line and your notes, answering the question when there is one.\n"
)
HOW: dict[Kind, str] = {
    "clip": (
        "Give timestamps (m:ss) for what happens; quote on-screen text verbatim; for the sound, quote speech "
        "verbatim in its language (who speaks, when you can tell) and name music and notable noises; say so "
        "when there is no sound."
    ),
    "audio": (
        "Give timestamps (m:ss); quote speech verbatim in its language (who speaks and how); name music "
        "(genre, mood, lyrics) and notable sounds."
    ),
    "pdf": (
        "Cite page numbers (p. 3); quote headings and key text verbatim; note numbers, tables and figures."
    ),
}
NO_QUESTION = "No particular question: note what is worth talking about."


def medium(row: MessageMedia) -> Kind | None:
    """What a row is to the media pin, if anything: a clip, audio or a PDF."""
    known = known_type(row.mime_type)
    match row.type:
        case "video" | "animation" | "video_note":
            return "clip"
        case "sticker" if known == "video/webm":
            return "clip"
        case "audio" | "voice":
            return "audio"
        case "document" if known == "application/pdf":
            return "pdf"
        case _:
            return None


def label(row: MessageMedia) -> str:
    """How the tool result names a file: "video 0:42", "voice 0:07", "file report.pdf"."""
    if row.type == "document":
        return f"file {row.file_name}" if row.file_name else "file"
    return kind_label(row.type) + duration_label(row.duration)


def _media_type(row: MessageMedia, kind: Kind) -> str | None:
    known = known_type(row.mime_type)
    if kind == "clip":
        return VIDEO_TYPES.get(known or "video/mp4")  # video messages and GIFs come without one: MP4
    if kind == "audio":
        audio = known or ("audio/ogg" if row.type == "voice" else None)
        return audio if audio in AUDIO_FORMATS else None
    return "application/pdf"


PAGE = re.compile(rb"/Type\s*/Page(?![A-Za-z])")
STREAM = re.compile(rb"stream\r?\n")
OBJECT_STREAM_BYTES = 8 * 2**20


def pdf_pages(data: bytes) -> int | None:
    """Page objects of a PDF, in the file and in its compressed object streams (PDF 1.5+); None: none found."""
    pages = len(PAGE.findall(data))
    for match in STREAM.finditer(data):
        head = data[max(0, match.start() - 300) : match.start()]
        if b"/ObjStm" not in head[head.rfind(b"<<") :] or b"/FlateDecode" not in head:
            continue
        try:
            body = zlib.decompressobj().decompress(data[match.end() :], OBJECT_STREAM_BYTES)
        except zlib.error:
            continue
        pages += len(PAGE.findall(body))
    return pages or None


@dataclass(slots=True)
class Look:
    """One file's whole-file look: the notes, or why there are none."""

    row: MessageMedia
    kind: Kind
    notes: str = ""
    why: str = ""
    pages: int | None = None

    def line(self) -> str:
        what = label(self.row) + (f", {self.pages} pages" if self.pages else "")
        if self.notes:
            sound = " with sound" if self.row.type in SOUNDTRACKS else ""  # GIFs and video stickers have none
            return f"#{self.row.message_id} ({what}, {VERBS[self.kind]}{sound}):\n{self.notes}"
        return f"#{self.row.message_id} ({what}): not {VERBS[self.kind]} — {self.why}."


def _limit(limits: WatchLimits, kind: Kind) -> int:
    return {"clip": limits.clip_seconds, "audio": limits.audio_seconds, "pdf": limits.pdf_pages}[kind]


def _too_long(kind: Kind, size: int, deps: RunDeps) -> str | None:
    """Why the file is past this tier's limit, if it is (naming the paid limit a sponsored answer lacks)."""
    watching = deps.settings.watching
    paid = deps.funding is Funding.PAID
    limit = _limit(watching.paid if paid else watching.sponsored, kind)
    if size <= limit:
        return None

    def shown(n: int) -> str:
        return f"{n} pages" if kind == "pdf" else duration_label(n).strip()

    why = f"longer than the {shown(limit)} this answer can take"
    if not paid and size <= (most := _limit(watching.paid, kind)):
        why += f" (paid answers take up to {shown(most)})"
    return why


async def _price(deps: RunDeps, input_tokens: int) -> Decimal:
    watching = deps.settings.watching
    async with deps.db.session() as session:
        prices = await session.get(ModelPin, ModelRole.MEDIA.value)
    prompt, completion = watching.price_usd_per_m_input, watching.price_usd_per_m_output
    if prices is not None and prices.price_prompt is not None:
        prompt, completion = float(prices.price_prompt), float(prices.price_completion or 0)
    return estimate_cost(input_tokens, watching.output_tokens, prompt, completion)


async def watch(ctx: RunContext[RunDeps], row: MessageMedia, kind: Kind, question: str) -> Look:
    """Send one file and the question to the media pin; ``Look.why`` says why not when it isn't sent."""
    deps = ctx.deps
    watching = deps.settings.watching
    look = Look(row, kind)
    media_type = _media_type(row, kind)
    seconds = row.duration or (STICKER_SECONDS if row.type == "sticker" else None)
    if deps.funding is Funding.FREE:
        look.why = "free models can't watch clips, listen to audio or read PDFs"
    elif media_type is None:
        look.why = "its format can't be sent to the viewer"
    elif row.file_size and row.file_size > BOT_API_DOWNLOAD_LIMIT:
        look.why = "it's bigger than the 20 MB Telegram lets bots download"
    elif kind != "pdf" and seconds is None:
        look.why = "its length is unknown"
    elif kind != "pdf" and seconds is not None and (why := _too_long(kind, seconds, deps)):
        look.why = why
    if look.why or media_type is None:
        return look
    try:
        data = await deps.gateway.fetch(row.file_id, row.bot_id, row.file_unique_id, row.file_size)
    except TooLargeError:
        look.why = "it's bigger than the 20 MB Telegram lets bots download"
        return look
    except MediaUnavailableError as unavailable:
        look.why = f"it can't be downloaded ({unavailable.reason})"
        return look
    if kind == "pdf":
        look.pages = await asyncio.to_thread(pdf_pages, data)
        if look.pages is None:
            look.why = "its pages couldn't be counted"
            return look
        if why := _too_long(kind, look.pages, deps):
            look.why = why
            return look
        units = look.pages * watching.pdf_tokens_per_page
    else:
        rate = watching.video_tokens_per_second if kind == "clip" else watching.audio_tokens_per_second
        units = (seconds or 0) * rate
    input_tokens = watching.prompt_tokens + units
    price = deps.settings.credits_for_usd(float(await _price(deps, input_tokens)))
    if (left := await hold_left(ctx, next_turn=True)) is not None and price > left:
        look.why = f"it would cost about {price} Derpcoins, more than this answer has left"
        return look
    pin = await deps.pins.get(ModelRole.MEDIA)
    agent = Agent(
        deps.model_factory(pin),
        instructions=INSTRUCTIONS + HOW[kind],
        model_settings=chat_settings(pin, max_tokens=watching.output_tokens, documents=kind == "pdf"),
    )
    text = ""
    with capture_run_messages() as captured:
        try:
            async with asyncio.timeout(watching.timeout_seconds):
                result = await agent.run(
                    [
                        f"The question: {question}" if question else NO_QUESTION,
                        BinaryContent(data, media_type=media_type),
                    ]
                )
            text = result.output.strip()
        except Exception as error:  # noqa: BLE001 - a failed look falls back to a still frame
            log.info("media look failed: %s", type(error).__name__)
    await ledger_side_call(
        deps.db, captured, kind="media", funding=deps.funding.value, role=ModelRole.MEDIA,
        context_tokens=input_tokens, run_id=deps.run_id, chat_id=deps.chat.id, user_id=deps.user_id,
        requested_model=pin.model,
    )  # fmt: skip
    first, _, rest = text.partition("\n")
    summary = " ".join(first.split())[:300]
    if not summary:
        look.why = "the viewer gave no answer"
        return look
    look.notes = "\n".join(part for part in (summary, rest.strip()) if part)
    if row.description is None:
        async with deps.db.transaction() as session:
            await store_description(session, row, summary, deps.run_id)
        row.description = summary
    return look
