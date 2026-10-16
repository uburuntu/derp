from decimal import Decimal
from typing import Any

from sqlalchemy import select

from derp.history.models import Message, MessageMedia
from derp.history.transcription import Transcriber
from derp.inference.api import OpenRouterError, Transcript
from derp.inference.models import ProviderCall
from tests.e2e.harness import Conversation, Derived, Person, Room
from tests.e2e.model import ScriptedModel

OGG = b"OggS" + b"\x00" * 60
EVA = Person(701, "Eva", language_code="ru")


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
        lambda s: Transcriber(db=s.db, api=stt, gateway=s.media, pins=s.pins, settings=s.settings)  # type: ignore[arg-type]
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
    assert transcripts == {short.message_id: "купи хлеба", long.message_id: None}


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
