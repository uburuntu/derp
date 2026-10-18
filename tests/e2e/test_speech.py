"""Derp acts a line out as a voice message on request (the speech pin is faked; ffmpeg is real)."""

import subprocess
from dataclasses import dataclass, field
from decimal import Decimal
from functools import cache

from sqlalchemy import func, select

from derp.agent.models import AgentRun, RunOutput
from derp.config import Settings
from derp.history.models import MessageMedia
from derp.inference.api import OpenRouterError, Speech
from derp.inference.models import ProviderCall
from derp.money.models import RunFee
from derp.money.wallets import balance, credit, wallet
from tests.e2e.harness import Conversation, Person, Room
from tests.e2e.model import ScriptedModel, tool_call
from tests.e2e.test_effects import needs_ffmpeg, results_of

MAX = Person(1901, "Max")
YARD = Room(-1019, title="Yard")
SCRIPT = "[shouting] ПРОД УПАЛ! [laughs harder] Шучу."


@cache
def mp3() -> bytes:
    args = ["-f", "lavfi", "-i", "sine=frequency=220:duration=1", "-c:a", "libmp3lame", "-f", "mp3"]
    command = ["ffmpeg", "-v", "error", *args, "pipe:1"]
    return subprocess.run(command, check=True, capture_output=True).stdout  # noqa: S603 - fixed arguments


@dataclass
class FakeSpeech:
    """Stands in for OpenRouterAPI's speech call: $20 per 1M characters, like v4 Turbo."""

    lines: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])

    async def speak(self, model: str, text: str, voice: str, **_: object) -> Speech:
        self.lines.append((model, text, voice))
        return Speech(mp3(), model, Decimal("0.00002") * len(text), len(text), "gen-tts-1")

    async def close(self) -> None:
        return None


async def voiced(conversation: Conversation) -> FakeSpeech:
    speech = FakeSpeech()
    conversation.overrides["api"] = speech
    await conversation.restart()
    return speech


async def fund(conversation: Conversation, credits: int) -> int:
    async with conversation.db.transaction() as session:
        wallet_id = (await wallet(session, "user", MAX.id)).id
        await credit(session, wallet_id, "stars:max", credits, "purchase")
    return wallet_id


@needs_ffmpeg
async def test_a_yelled_line_goes_out_as_a_voice_message_charged_by_length(
    conversation: Conversation, model: ScriptedModel, settings: Settings
) -> None:
    speech = await voiced(conversation)
    wallet_id = await fund(conversation, 650)
    model.reply(tool_call("speak", script=SCRIPT, voice="harry"), ".")
    asked = await conversation.send(MAX, text="yell at the team that prod is down")
    sent = await conversation.expect_reply(asked, method="sendVoice", timeout=30)
    await conversation.settle(asked, timeout=30)
    assert sent.attachments[0].data[:4] == b"OggS"
    assert speech.lines == [("elevenlabs/eleven-v4-turbo", SCRIPT, "harry")]
    assert "Sent it as #" in results_of(model, "speak")[0]
    assert conversation.api.calls_of("sendRichMessage", after=asked.after) == []  # "." sends nothing else
    cost = Decimal("0.00002") * len(SCRIPT)
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun))
        call = await session.scalar(select(ProviderCall).where(ProviderCall.kind == "speech"))
        assert run is not None and call is not None
        assert (call.run_id, call.funding, call.role) == (run.id, "paid", "speech")
        assert (call.units, call.cost_usd, call.generation_id) == (len(SCRIPT), cost, "gen-tts-1")
        assert run.charged_credits == settings.credits_for_usd(float(cost))  # metered, no flat fee
        assert await balance(session, wallet_id) == 650 - run.charged_credits
        assert await session.scalar(select(RunOutput.kind)) == "other"
        assert await session.scalar(select(RunFee.id)) is None
        media = await session.scalar(select(MessageMedia).where(MessageMedia.type == "voice"))
        assert media is not None and (media.transcript, media.transcript_model) == (
            SCRIPT,
            "elevenlabs/eleven-v4-turbo",
        )  # Derp knows what it said; nobody pays to transcribe it


@needs_ffmpeg
async def test_sponsored_voice_messages_count_as_heavy_effects(
    conversation: Conversation, model: ScriptedModel, settings: Settings
) -> None:
    speech = await voiced(conversation)
    conversation.meet(MAX, YARD)
    async with conversation.db.transaction() as session:
        for _ in range(settings.sponsored_effects_per_day - 1):
            session.add(
                RunFee(run_id=0, chat_id=YARD.id, user_id=MAX.id, item="meme", funding="sponsored", credits=0)
            )
    model.reply(tool_call("speak", script="[whispers] тсс", voice="lily"), ".")
    asked = await conversation.send(MAX, YARD, "derp whisper something")
    await conversation.expect_reply(asked, method="sendVoice", timeout=30)
    await conversation.settle(asked, timeout=30)
    async with conversation.db.session() as session:
        fee = await session.scalar(select(RunFee).where(RunFee.item == "voice"))
        assert fee is not None and (fee.funding, fee.credits) == ("sponsored", 0)
        call = await session.scalar(select(ProviderCall).where(ProviderCall.kind == "speech"))
        assert call is not None and call.funding == "sponsored" and call.run_id is not None

    model.reply(tool_call("speak", script="[sings] ещё!"), "out of voice today")
    again = await conversation.send(MAX, YARD, "derp sing it")
    await conversation.expect_reply(again)
    await conversation.settle(again)
    assert "used up" in results_of(model, "speak")[-1]  # the first run is replayed before it
    assert len(speech.lines) == 1 and conversation.api.calls_of("sendVoice", after=again.after) == []


async def test_a_script_over_the_cap_is_refused_before_any_call(
    conversation: Conversation, model: ScriptedModel, settings: Settings
) -> None:
    speech = await voiced(conversation)
    await fund(conversation, 650)
    model.reply(tool_call("speak", script="а" * (settings.speech.max_chars + 1)), "too long")
    asked = await conversation.send(MAX, text="read the whole manifesto out loud")
    await conversation.expect_reply(asked)
    await conversation.settle(asked)
    assert "Too long for a voice message" in results_of(model, "speak")[0]
    assert speech.lines == []


@needs_ffmpeg
async def test_a_voice_message_telegram_refused_is_absorbed(
    conversation: Conversation, model: ScriptedModel
) -> None:
    await voiced(conversation)
    await fund(conversation, 650)
    conversation.api.fail_next(
        "sendVoice", error_code=400, description="Bad Request: VOICE_MESSAGES_FORBIDDEN"
    )
    model.reply(tool_call("speak", script="[sighs] ну ладно"), "can't send voice here")
    asked = await conversation.send(MAX, text="sigh at me")
    await conversation.expect_reply(asked)
    await conversation.settle(asked, timeout=30)
    assert "Nothing was charged for it" in results_of(model, "speak")[0]
    async with conversation.db.session() as session:
        call = await session.scalar(select(ProviderCall).where(ProviderCall.kind == "speech"))
        assert call is not None and (call.funding, call.run_id) == ("creator", None)


@needs_ffmpeg
async def test_a_kept_voice_message_can_be_played_backwards(
    conversation: Conversation, model: ScriptedModel
) -> None:
    await voiced(conversation)
    await fund(conversation, 650)
    model.reply(
        tool_call("speak", script="[laughs] ха-ха", voice="jessica", final=False),
        tool_call("reverse_media", from_result="r1"),
        ".",
    )
    asked = await conversation.send(MAX, text="laugh, but backwards")
    await conversation.expect_reply(asked, method="sendVoice", timeout=30)
    await conversation.settle(asked, timeout=30)
    assert len(conversation.api.calls_of("sendVoice", after=asked.after)) == 1  # only the chain's end
    async with conversation.db.session() as session:
        run_id = await session.scalar(select(AgentRun.id))
        speech_calls = await session.scalar(
            select(func.count()).where(ProviderCall.kind == "speech", ProviderCall.run_id == run_id)
        )
        assert speech_calls == 1  # the voice line travelled with the reversed result and was charged once


@needs_ffmpeg
async def test_derps_own_voice_message_is_read_from_its_script_never_listened_to(
    conversation: Conversation, model: ScriptedModel
) -> None:
    await voiced(conversation)
    await fund(conversation, 650)
    model.reply(tool_call("speak", script=SCRIPT, voice="harry"), ".")
    asked = await conversation.send(MAX, text="yell at the team that prod is down")
    await conversation.expect_reply(asked, method="sendVoice", timeout=30)
    await conversation.settle(asked, timeout=30)
    async with conversation.db.session() as session:
        voice_id = await session.scalar(select(MessageMedia.message_id).where(MessageMedia.type == "voice"))
    model.reply(tool_call("inspect_media", message_id=voice_id, question="what did you yell?"), "that")
    again = await conversation.send(MAX, text="wait, what did you yell?")
    await conversation.expect_reply(again)
    await conversation.settle(again)
    result = results_of(model, "inspect_media")[-1]
    assert f"#{voice_id} (voice" in result and "Derp's own voice message" in result and SCRIPT in result
    async with conversation.db.session() as session:
        assert await session.scalar(select(ProviderCall.id).where(ProviderCall.kind == "media")) is None


@dataclass
class TimingOut(FakeSpeech):
    """The primary model's call times out (it may still have been made and billed)."""

    async def speak(self, model: str, text: str, voice: str, **_: object) -> Speech:
        self.lines.append((model, text, voice))
        raise OpenRouterError(0, "ReadTimeout")


async def test_a_timed_out_voice_line_is_absorbed_and_never_tried_again_elsewhere(
    conversation: Conversation, model: ScriptedModel, settings: Settings
) -> None:
    speech = TimingOut()
    conversation.overrides["api"] = speech
    await conversation.restart()
    await fund(conversation, 650)
    model.reply(tool_call("speak", script="[yells] ЭЙ"), "my voice broke")
    asked = await conversation.send(MAX, text="yell hey")
    await conversation.expect_reply(asked)
    await conversation.settle(asked)
    assert speech.lines == [("elevenlabs/eleven-v4-turbo", "[yells] ЭЙ", "callum")]  # no fallback call
    assert results_of(model, "speak")[0] == "The voice message timed out; nothing was charged for it."
    estimate = Decimal(str(settings.speech.list_price("elevenlabs/eleven-v4-turbo") * 10 / 1_000_000))
    async with conversation.db.session() as session:
        call = await session.scalar(select(ProviderCall).where(ProviderCall.kind == "speech"))
        assert call is not None and (call.funding, call.run_id, call.units) == ("creator", None, 10)
        assert call.cost_usd == estimate
        assert await session.scalar(select(RunFee.id)) is None
