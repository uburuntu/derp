import asyncio
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest
from sqlalchemy import select

from derp.history.models import SILENT, TOO_BIG, TOO_LONG, UNHEARD, Message, MessageMedia
from derp.history.transcription import Transcriber
from derp.inference.api import OpenRouterError, Transcript
from derp.inference.models import ProviderCall
from derp.ops.owner import notify_owners
from tests.e2e.harness import Conversation, Derived, Person, Room
from tests.e2e.model import ScriptedModel

OGG = b"OggS" + b"\x00" * 60
EVA = Person(701, "Eva", language_code="ru")
OWNER = Person(1000, "Owner")


class FakeSTT:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.calls: list[tuple[str, bytes, str]] = []
        self.fail_first = fail_first

    async def transcribe(self, model: str, audio: bytes, audio_format: str, **_: Any) -> Transcript:
        self.calls.append((model, audio, audio_format))
        if self.fail_first and len(self.calls) == 1:
            raise OpenRouterError(502, "upstream down")
        return Transcript(text="купи хлеба", model=model, cost_usd=Decimal("0.000015"))


def voice(file_id: str, duration: int = 5) -> dict[str, object]:
    return {
        "file_id": file_id,
        "file_unique_id": f"u-{file_id}",
        "duration": duration,
        "mime_type": "audio/ogg",
    }


async def use_stt(conversation: Conversation, stt: FakeSTT) -> None:
    conversation.overrides["transcriber"] = Derived(
        lambda s: Transcriber(
            db=s.db,
            api=stt,  # type: ignore[arg-type]
            gateway=s.media,
            pins=s.pins,
            workers=s.workers,
            settings=s.settings,
            notify=lambda text: notify_owners(s.bot, s.settings, text),
        )
    )
    await conversation.restart()


async def test_private_voice_is_transcribed_before_derp_answers(
    conversation: Conversation, model: ScriptedModel
) -> None:
    stt = FakeSTT(fail_first=True)  # whisper fails → the fallback model answers
    await use_stt(conversation, stt)
    conversation.api.register_file("v1", OGG)
    model.reply("хлеб куплю мысленно")
    sent = await conversation.send(EVA, voice=voice("v1"))
    await conversation.expect_reply(sent)
    await conversation.settle(sent)
    assert [c[0] for c in stt.calls] == ["openai/whisper-large-v3-turbo", "elevenlabs/scribe-v2"]
    assert stt.calls[-1][1:] == (OGG, "ogg")
    prompt = str(model.requests[-1][-1])
    assert "[voice 0:05: купи хлеба]" in prompt
    async with conversation.db.session() as session:
        media = await session.scalar(select(MessageMedia).where(MessageMedia.message_id == sent.message_id))
        assert (
            media is not None
            and media.transcript == "купи хлеба"
            and media.transcript_model == "elevenlabs/scribe-v2"
        )
        row = await session.get(Message, (EVA.id, sent.message_id))
        assert row is not None and row.extra_text == "купи хлеба"
        call = await session.scalar(select(ProviderCall).where(ProviderCall.kind == "stt"))
        assert call is not None and (call.funding, call.units, call.user_id) == ("creator", 5, EVA.id)


async def test_ambient_voice_is_transcribed_silently_and_long_ones_are_skipped(
    conversation: Conversation, model: ScriptedModel
) -> None:
    stt = FakeSTT()
    await use_stt(conversation, stt)
    group = Room(-1008, title="Voices")
    conversation.meet(EVA, group)
    conversation.api.register_file("v2", OGG)
    conversation.api.register_file("v3", OGG)
    short = await conversation.send(EVA, group, voice=voice("v2"))
    long = await conversation.send(EVA, group, voice=voice("v3", duration=3600))
    await conversation.settle(long)
    assert model.requests == []  # nothing is said
    assert len(stt.calls) == 1
    async with conversation.db.session() as session:
        rows = await session.execute(select(MessageMedia.message_id, MessageMedia.transcript))
        transcripts = {row[0]: row[1] for row in rows}
    assert transcripts == {short.message_id: "купи хлеба", long.message_id: ""}  # too long: settled empty


class BrokenSTT(FakeSTT):
    async def transcribe(self, model: str, audio: bytes, audio_format: str, **_: Any) -> Transcript:
        raise FileNotFoundError("ffmpeg")  # any failure, not only provider errors


async def test_voice_invocation_is_answered_even_when_transcription_breaks(
    conversation: Conversation, model: ScriptedModel
) -> None:
    await use_stt(conversation, BrokenSTT())
    conversation.api.register_file("v2", OGG)
    model.reply("не расслышал, но я тут")
    sent = await conversation.send(EVA, voice=voice("v2"))
    reply = await conversation.expect_reply(sent)
    assert reply["rich_message"]["markdown"] == "не расслышал, но я тут"


async def test_ambient_voice_stops_at_the_daily_cap_but_invocations_still_hear_it(
    conversation: Conversation, model: ScriptedModel
) -> None:
    stt = FakeSTT()
    await use_stt(conversation, stt)
    conversation.meet(OWNER)
    group = Room(-1009, title="Chatty")
    conversation.meet(EVA, group)
    async with conversation.db.transaction() as session:
        cap = Decimal(str(conversation.settings.ambient_stt_daily_usd))
        session.add(ProviderCall(kind="stt", model="m", funding="creator", cost_usd=cap))
    for file_id in ("v4", "v5"):
        conversation.api.register_file(file_id, OGG)
    first = await conversation.send(EVA, group, voice=voice("v4"))
    second = await conversation.send(EVA, group, voice=voice("v5"))
    await conversation.settle(second)
    assert stt.calls == []  # over the cap: ambient voice is kept untranscribed
    pings = [c for c in conversation.api.calls if c.method == "sendMessage" and int(c["chat_id"]) == OWNER.id]
    assert len(pings) == 1 and "cap" in str(pings[0]["text"])  # once per day, not per message

    model.reply("хлеба нет")
    asked = await conversation.send(EVA, group, "дерп, что она сказала?", reply_to=first.message_id)
    await conversation.expect_reply(asked)
    assert len(stt.calls) == 1  # the invocation needs it: transcribed on demand
    assert "купи хлеба" in str(model.requests[-1])


def audio(file_id: str, duration: int = 20, mime_type: str = "audio/ogg") -> dict[str, object]:
    return {
        "file_id": file_id,
        "file_unique_id": f"u-{file_id}",
        "duration": duration,
        "mime_type": mime_type,
    }


async def test_an_audio_file_sent_to_derp_is_transcribed(
    conversation: Conversation, model: ScriptedModel
) -> None:
    stt = FakeSTT()
    await use_stt(conversation, stt)
    conversation.api.register_file("a1", OGG)
    model.reply("куплю")
    sent = await conversation.send(EVA, audio=audio("a1"))
    await conversation.expect_reply(sent)
    assert stt.calls[-1][1:] == (OGG, "ogg")  # OGG goes as is
    assert "[audio 0:20: купи хлеба]" in str(model.requests[-1][-1])


async def test_ambient_audio_files_wait_until_someone_asks(
    conversation: Conversation, model: ScriptedModel
) -> None:
    stt = FakeSTT()
    await use_stt(conversation, stt)
    group = Room(-1011, title="Memos")
    conversation.meet(EVA, group)
    conversation.api.register_file("a2", OGG)
    conversation.api.register_file("a3", OGG)
    memo = await conversation.send(EVA, group, audio=audio("a2"))
    long = await conversation.send(EVA, group, audio=audio("a3", duration=3600))
    await conversation.settle(long)
    assert stt.calls == []  # audio nobody asked about is never transcribed

    model.reply("хлеб")
    asked = await conversation.send(EVA, group, "дерп, что там?", reply_to=memo.message_id)
    await conversation.expect_reply(asked)
    assert len(stt.calls) == 1 and "купи хлеба" in str(model.requests[-1])
    model.reply("не осилю")
    asked = await conversation.send(EVA, group, "дерп, а тут?", reply_to=long.message_id)
    await conversation.expect_reply(asked)
    assert len(stt.calls) == 1 and "[audio 60:00 — too long to transcribe]" in str(model.requests[-1])


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_audio_in_other_formats_is_converted_first(
    conversation: Conversation, model: ScriptedModel, tmp_path: Path
) -> None:
    stt = FakeSTT()
    await use_stt(conversation, stt)
    target = tmp_path / "memo.mp3"
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(target),
    )  # fmt: skip
    assert await process.wait() == 0
    conversation.api.register_file("a4", target.read_bytes())
    model.reply("ok")
    sent = await conversation.send(EVA, audio=audio("a4", duration=1, mime_type="audio/mpeg"))
    await conversation.expect_reply(sent)
    assert stt.calls[-1][1].startswith(b"OggS") and stt.calls[-1][2] == "ogg"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
async def test_the_speech_in_a_video_is_heard_once_someone_asks(
    conversation: Conversation, model: ScriptedModel, tmp_path: Path
) -> None:
    stt = FakeSTT()
    await use_stt(conversation, stt)
    group = Room(-1012, title="Clips")
    conversation.meet(EVA, group)
    target = tmp_path / "clip.mp4"
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", "-shortest", "-c:v", "libx264", "-c:a", "aac", str(target),
    )  # fmt: skip
    assert await process.wait() == 0
    conversation.api.register_file("v1", target.read_bytes())
    video = {"file_id": "v1", "file_unique_id": "u-v1", "width": 64, "height": 64, "duration": 1}
    clip = await conversation.send(EVA, group, video=video)
    await conversation.settle(clip)
    assert stt.calls == []  # videos nobody asked about are never transcribed

    model.reply("хлеб")
    asked = await conversation.send(EVA, group, "дерп, что он сказал?", reply_to=clip.message_id)
    await conversation.expect_reply(asked)
    assert len(stt.calls) == 1 and stt.calls[0][1].startswith(b"OggS") and stt.calls[0][2] == "ogg"
    assert "[video 0:01: купи хлеба]" in str(model.requests[-1])

    huge = {**video, "file_id": "v2", "file_unique_id": "u-v2", "file_size": 21 * 2**20}
    model.reply("не скачать")
    sent = await conversation.send(EVA, group, caption="дерп, а тут?", video=huge)
    await conversation.expect_reply(sent)
    assert len(stt.calls) == 1  # past Telegram's download limit: never fetched
    assert "[video 0:01 — too big to download]" in str(model.requests[-1])

    silent = tmp_path / "silent.mp4"
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", "-c:v", "libx264",
        str(silent),
    )  # fmt: skip
    assert await process.wait() == 0
    conversation.api.register_file("v3", silent.read_bytes())
    model.reply("тишина")
    mute = {**video, "file_id": "v3", "file_unique_id": "u-v3"}
    sent = await conversation.send(EVA, group, caption="дерп, что тут говорят?", video=mute)
    await conversation.expect_reply(sent)
    assert len(stt.calls) == 1 and "[video 0:01 — no sound]" in str(model.requests[-1])
    async with conversation.db.session() as session:
        reasons = set(await session.scalars(select(MessageMedia.transcript_model)))
    assert {TOO_BIG, SILENT} <= reasons  # settled: never fetched or probed again


async def test_derp_is_told_why_a_voice_note_has_no_transcript(
    conversation: Conversation, model: ScriptedModel
) -> None:
    await use_stt(conversation, BrokenSTT())
    conversation.api.register_file("v6", OGG)
    conversation.api.register_file("v7", OGG)
    model.reply("не расслышал")
    broken = await conversation.send(EVA, voice=voice("v6"))
    await conversation.expect_reply(broken)
    assert "[voice 0:05 — couldn't transcribe]" in str(model.requests[-1][-1])
    model.reply("длинно")
    long = await conversation.send(EVA, voice=voice("v7", duration=724))
    await conversation.expect_reply(long)
    assert "[voice 12:04 — too long to transcribe]" in str(model.requests[-1][-1])
    async with conversation.db.session() as session:
        rows = (await session.execute(select(MessageMedia.transcript, MessageMedia.transcript_model))).all()
    assert set(rows) == {("", UNHEARD), ("", TOO_LONG)}  # tried, nothing: empty, like an empty image note


class WatchedTranscriber(Transcriber):
    settled: ClassVar[list[tuple[int, int | None, int]]] = []

    async def settle(self, chat_id: int, thread_id: int | None, before: int, *, limit: float = 5.0) -> None:
        self.settled.append((chat_id, thread_id, before))
        await super().settle(chat_id, thread_id, before, limit=limit)


async def test_runs_settle_pending_transcriptions_before_reading_the_chat(
    conversation: Conversation, model: ScriptedModel
) -> None:
    stt = FakeSTT()
    conversation.overrides["transcriber"] = Derived(
        lambda s: WatchedTranscriber(
            db=s.db,
            api=stt,  # type: ignore[arg-type]
            gateway=s.media,
            pins=s.pins,
            workers=s.workers,
            settings=s.settings,
        )
    )
    await conversation.restart()
    model.reply("hi")
    sent = await conversation.send(EVA, text="answer the voice note")
    await conversation.expect_reply(sent)
    assert WatchedTranscriber.settled[-1] == (EVA.id, None, sent.message_id)
