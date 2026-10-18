import asyncio
import io
import zlib
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from derp.agent.deps import RunDeps
from derp.agent.watch import Look, label, medium, pdf_pages, watch
from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import MessageMedia
from derp.inference.models import ProviderCall
from derp.inference.roles import RolePin
from derp.money.funding import Funding, Funds


def row(kind: str, **fields: object) -> MessageMedia:
    return MessageMedia(
        chat_id=1, message_id=7, idx=0, type=kind, file_id="f", file_unique_id="u-f", bot_id=1, **fields
    )


def test_clips_audio_and_pdfs_go_to_the_media_pin_and_nothing_else() -> None:
    assert medium(row("video")) == medium(row("animation")) == medium(row("video_note")) == "clip"
    assert medium(row("sticker", mime_type="video/webm")) == "clip"
    assert medium(row("audio", mime_type="audio/mpeg")) == medium(row("voice")) == "audio"
    assert medium(row("document", mime_type="application/pdf")) == "pdf"
    assert medium(row("sticker", mime_type="application/x-tgsticker")) is None
    assert medium(row("document", mime_type="application/zip")) is None
    assert medium(row("photo")) is None


def test_lines_name_the_file_and_say_why_it_was_not_opened() -> None:
    clip = Look(row("video", duration=95), "clip", why="longer than the 1:00 this answer can take")
    assert clip.line() == "#7 (video 1:35): not watched — longer than the 1:00 this answer can take."
    paper = Look(row("document", file_name="a.pdf"), "pdf", notes="A thesis.\n(p. 2) Results", pages=3)
    assert paper.line() == "#7 (file a.pdf, 3 pages, read):\nA thesis.\n(p. 2) Results"
    assert label(row("voice", duration=7)) == "voice 0:07"
    own = Look(row("voice", duration=3), "audio", notes="[yells] HEY", script=True)
    assert own.line() == "#7 (voice 0:03, Derp's own voice message; its script):\n[yells] HEY"


def test_pdf_pages_are_counted_in_the_file_and_in_compressed_object_streams() -> None:
    pages = [Image.new("RGB", (60, 80), "white") for _ in range(3)]
    buffer = io.BytesIO()
    pages[0].save(buffer, format="PDF", save_all=True, append_images=pages[1:])
    assert pdf_pages(buffer.getvalue()) == 3
    objects = zlib.compress(b"3 0 4 20 << /Type /Page /Parent 2 0 R >> << /Type /Page /Parent 2 0 R >>")
    stream = (
        f"5 0 obj << /Type /ObjStm /N 2 /First 8 /Filter /FlateDecode /Length {len(objects)} >>\nstream\n"
    )
    packed = (
        b"%PDF-1.5\n2 0 obj << /Type /Pages /Count 2 >> endobj\n"
        + stream.encode()
        + objects
        + b"\nendstream endobj\n"
    )
    assert pdf_pages(packed) == 2  # the /Pages node itself isn't a page
    assert pdf_pages(b"%PDF-1.7\nnothing readable") is None


@pytest.mark.db
async def test_a_look_stopped_mid_call_is_ledgered_at_its_estimate_on_derp(
    database: Database, settings: Settings
) -> None:
    started = asyncio.Event()

    async def viewer(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        started.set()
        await asyncio.Event().wait()  # Stop is pressed before it answers
        raise AssertionError

    async def fetch(*_args: object) -> bytes:
        return b"OggS"

    pin = RolePin(model="google/flash-lite", reasoning=None)
    deps = SimpleNamespace(
        settings=settings, funding=Funding.PAID, funds=Funds(Funding.PAID, "turn:1", None, None, 0), db=database,
        gateway=SimpleNamespace(fetch=fetch), pins=SimpleNamespace(get=AsyncMock(return_value=pin)),
        model_factory=lambda _pin: FunctionModel(viewer), chat=SimpleNamespace(id=-1), user_id=1, run_id=5,
    )  # fmt: skip
    ctx = cast(RunContext[RunDeps], SimpleNamespace(deps=deps))
    look = asyncio.create_task(watch(ctx, row("audio", mime_type="audio/ogg", duration=10), "audio", ""))
    await started.wait()
    look.cancel()
    with pytest.raises(asyncio.CancelledError):
        await look
    async with database.session() as session:
        [call] = await session.scalars(select(ProviderCall))
    watching = settings.watching
    tokens = watching.prompt_tokens + 10 * watching.audio_tokens_per_second
    assert (call.kind, call.funding, call.run_id, call.model, call.input_tokens) == (
        "media", "creator", None, "google/flash-lite", tokens
    )  # fmt: skip
    assert call.cost_usd > 0
