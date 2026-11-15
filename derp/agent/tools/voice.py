"""Voice messages: Derp acts a short line out (yelling, whispering, singing, accents, sound effects).

The model writes the performance with inline audio tags and picks one of a few
stock voices. The speech pin (``ModelRole.SPEECH``) voices it on the first of
its models able to act it out (``voicing``, by ``settings.speech.models``): a
model without sound effects skips lines with any tag that isn't a delivery
direction, one weak in Russian skips Cyrillic, and one that would read tags
aloud gets them as a delivery note instead. The shared worker pool turns the
audio into OGG/Opus, and it goes out through ``derp.agent.outputs`` as a voice
message whose script is stored as its transcript. ``speak`` is one of the
effects tools (registered in ``derp.agent.tools``).

A line is priced per character (``rates``: the pin's price snapshot when the
primary is priced per character, list prices otherwise) at the dearest model
able to voice it, before it is made. The provider call travels with the
result and is ledgered against the run only once Telegram accepted it (or a
result made from it); otherwise it is absorbed. A call that timed out or was
stopped may still be billed, so it is ledgered at its estimate on Derp and
never followed by a second paid call on a fallback model. Paid runs pay its
characters; sponsored runs pay them from the day's allowance and count it as
one of the day's heavy effects.
"""

import asyncio
import io
import logging
import re
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, TypedDict

from pydantic_ai import RunContext

from derp.agent.deps import RunDeps
from derp.agent.outputs import Made, Pending, Refused, check_budget, emit, hold_left, settle
from derp.agent.tools.effects import heavy_allowed
from derp.agent.tools.sources import FinalArg
from derp.config import SpeechModel, VoiceMessages
from derp.history.capture import Spoken
from derp.inference.api import OpenRouterError, Speech
from derp.inference.ledger import Billed, record_usage, simple_usage
from derp.inference.pins import snapshot_prices
from derp.inference.roles import ModelRole, RolePin
from derp.money.funding import Funding
from derp.tools import effects
from derp.tools.workers import EffectError

log = logging.getLogger("derp.voice")
# Stock voices that act well in Russian and English; the tool's docstring describes each by sex, age and vibe.
Voice = Literal["bill", "george", "callum", "harry", "charlie", "liam", "lily", "matilda", "jessica", "river"]
# Each voice's character, for a model that takes a delivery note rather than ElevenLabs' stock voices.
CHARACTERS: dict[str, str] = {
    "bill": "a wise, grumpy old man",
    "george": "a warm, mature British storyteller",
    "callum": "a gravelly trickster",
    "harry": "a rough, fierce young man, made for yelling",
    "charlie": "a hyped young Australian bloke",
    "liam": "an energetic young man",
    "lily": "a velvety, theatrical woman",
    "matilda": "a warm middle-aged woman",
    "jessica": "a bright, playful, giggly young woman",
    "river": "a relaxed, neutral voice",
}
MAX_TURNS = 8


class Turn(TypedDict):
    """One line of a skit: who says it and how (audio tags work per turn)."""

    voice: Voice
    script: str


TAG = re.compile(r"\[([^\]]+)\]")
CYRILLIC = re.compile(r"[\u0400-\u04ff]")
# Tags a model without sound effects can still act from a delivery note: how the words are said. Any other
# tag ([laughs], [sighs], [fart], [applause]) is a sound, for a model that makes them.
DIRECTIONS = frozenset({
    "whispers", "whispering", "shouting", "shouts", "yelling", "yells", "screaming", "sarcastic", "deadpan",
    "excited", "angry", "furious", "sad", "calm", "serious", "dramatic", "dramatically", "cheerful", "happy",
    "nervous", "mischievous", "softly", "quietly", "loudly", "louder", "slowly", "quickly", "fast", "tired",
    "sleepy", "bored", "proud", "curious", "confused", "annoyed", "sing-song", "raspy", "grave", "weary",
    "storytelling", "normal", "menacing", "flirty", "smug", "robotic", "posh", "panicked", "croaky", "shaky",
    "hoarse", "husky", "breathy", "nasal", "gruff", "trembling", "slow", "elderly", "childlike",
})  # fmt: skip


def is_direction(tag: str) -> bool:
    """How the words are said: every comma-separated part a known direction, an accent, a voice or a tone
    ([strong French accent], [elderly grandmother voice, croaky])."""
    parts = [part.strip().lower() for part in tag.split(",")]
    return all(part in DIRECTIONS or part.endswith((" accent", " voice", " tone")) for part in parts)


def can_voice(model: SpeechModel, script: str) -> bool:
    """Whether ``model`` can act ``script`` out: its tags, its alphabet, and words to say."""
    words = TAG.sub(" ", script)
    if not model.cyrillic and CYRILLIC.search(words):
        return False
    if not model.sounds and not all(is_direction(tag) for tag in TAG.findall(script)):
        return False
    return model.tags == "inline" or bool(words.strip())


def voicing(speech: VoiceMessages, pin: RolePin, script: str) -> tuple[str, ...]:
    """The pin's models able to act ``script`` out, in the pin's order."""
    return tuple(m for m in pin.models if can_voice(speech.model(m), script))


@dataclass(frozen=True, slots=True)
class Line:
    """A script as one model is asked to voice it."""

    text: str
    voice: str
    instructions: str | None = None


def line_for(model: SpeechModel, script: str, voice: str) -> Line:
    """Inline tags stay put; a model that would read them aloud gets them, with the voice's character, as a
    delivery note."""
    name = model.voices.get(voice, voice)
    if model.tags == "inline":
        return Line(script, name)
    tags = [tag.strip() for tag in TAG.findall(script)]
    note = CHARACTERS.get(voice, "an expressive voice actor")
    return Line(
        " ".join(TAG.sub(" ", script).split()), name, f"Act it as {note}" + "".join(f"; {t}" for t in tags)
    )


def _frames(mp3: bytes) -> bytes:
    """An MP3 without its ID3v2 tag, so it can follow another one mid-stream."""
    if mp3[:3] != b"ID3" or len(mp3) < 10:
        return mp3
    size = (mp3[6] & 0x7F) << 21 | (mp3[7] & 0x7F) << 14 | (mp3[8] & 0x7F) << 7 | (mp3[9] & 0x7F)
    footer = 10 if mp3[5] & 0x10 else 0
    return mp3[10 + size + footer :]


def joined(clips: Sequence[bytes]) -> bytes:
    """A skit's turns as one recording: WAVs (raw-PCM models) merged frame by frame, MP3s back to back (a
    decoder plays concatenated MP3 frames as one stream)."""
    if len(clips) == 1:
        return clips[0]
    if not all(clip[:4] == b"RIFF" for clip in clips):
        return clips[0] + b"".join(_frames(clip) for clip in clips[1:])
    out, frames = io.BytesIO(), b""
    with wave.open(io.BytesIO(clips[0])) as first:
        params = first.getparams()
    for clip in clips:
        with wave.open(io.BytesIO(clip)) as file:
            frames += file.readframes(file.getnframes())
    with wave.open(out, "wb") as file:
        file.setparams(params)
        file.writeframes(frames)
    return out.getvalue()


@dataclass(frozen=True, slots=True)
class Rates:
    """The speech pin and USD per 1M characters for each of its models."""

    pin: RolePin
    usd_per_m: dict[str, float]

    def usd(self, model: str, characters: int) -> Decimal:
        return Decimal(str(self.usd_per_m[model] * characters / 1_000_000)).quantize(Decimal("0.00000001"))

    def most(self, characters: int, models: Sequence[str] = ()) -> Decimal:
        """What a line costs at most: on the dearest of ``models`` (default all) that may voice it."""
        return max(self.usd(model, characters) for model in models or self.usd_per_m)


async def rates(deps: RunDeps) -> Rates:
    """The primary at its price snapshot (OpenRouter's price as the scout last saw it) when that is per
    character; the other models, a primary not yet snapshotted and a token-priced one at the list prices in
    ``settings.speech``."""
    speech = deps.settings.speech
    pin = await deps.pins.get(ModelRole.SPEECH)
    listed = speech.list_price(pin.model)
    async with deps.db.session() as session:
        prompt, completion = await snapshot_prices(session, ModelRole.SPEECH, (listed, 0.0))
    primary = prompt if completion == 0 else listed  # a completion price: per token, not per character
    return Rates(pin, {m: primary if m == pin.model else speech.list_price(m) for m in pin.models})


async def _absorb(deps: RunDeps, model: str, cost: Decimal, characters: int) -> None:
    """A line that may still be billed with nothing to show for it (timed out, stopped): its estimate, on Derp."""
    async with deps.db.transaction() as session:
        record_usage(
            session, simple_usage(model, cost), kind="speech", funding="creator", role=ModelRole.SPEECH.value,
            chat_id=deps.chat.id, user_id=deps.user_id, units=characters,
        )  # fmt: skip


async def _perform(deps: RunDeps, prices: Rates, script: str, voice: str, models: Sequence[str]) -> Speech:
    """Voice the line on ``models`` in turn; a refused call moves on, one that may have been made
    (status 0: timed out or cut off; or stopped) is ledgered at its estimate and ends it."""
    error = OpenRouterError(503, "no speech model can act this line")
    for model in models:
        spec = deps.settings.speech.model(model)
        line = line_for(spec, script, voice)
        try:
            return await deps.api.speak(
                model, line.text, line.voice, cost_usd=prices.usd(model, len(script)), zdr=prices.pin.zdr,
                instructions=line.instructions, audio=spec.audio,
            )  # fmt: skip
        except OpenRouterError as failed:
            log.info("speech model failed: %s", failed.status)
            if failed.status == 0:
                await _absorb(deps, model, prices.usd(model, len(script)), len(script))
                raise
            error = failed
        except asyncio.CancelledError:
            await _absorb(deps, model, prices.usd(model, len(script)), len(script))
            raise
    raise error


async def affordable(
    ctx: RunContext[RunDeps], prices: Rates, models: Sequence[str], characters: int
) -> tuple[str, ...]:
    """The models to try: the first able one must fit the hold (``Refused`` otherwise); a dearer fallback stays
    only while it fits too, so a line is never refused for what a fallback might cost."""
    coins = ctx.deps.settings.credits_for_usd
    await heavy_allowed(ctx, price=coins(float(prices.usd(models[0], characters))))
    left = await hold_left(ctx, next_turn=True)
    fits = [m for m in models[1:] if left is None or coins(float(prices.usd(m, characters))) <= left]
    return (models[0], *fits)


async def speak(
    ctx: RunContext[RunDeps],
    script: str = "",
    voice: Voice = "callum",
    final: FinalArg = True,
    dialogue: list[Turn] | None = None,
) -> str:
    """Act a short line out as a voice message. Only when someone asks to hear Derp: yell, whisper, sing, laugh,
    do an accent, a character or a sound effect. Never as a plain reading of an answer.

    Write it to be performed: audio tags in square brackets go before the words they colour, e.g. [shouting],
    [whispers], [laughs harder], [sighs], [sarcastic], [crying], [excited], [sings], [strong French accent], and
    sound effects such as [applause], [explosion], [gunshot], [fart]. CAPITALS add emphasis, "..." a pause.
    Tags are acted, never read out; the words go in the chat's language. For a character no voice covers (a
    granny, a little kid, a robot, a pirate), open with a casting tag on the closest voice:
    [elderly grandmother voice, croaky] on lily or matilda, [little kid voice] on jessica.

    Args:
        script: The performance with its audio tags; a few sentences at most (tags count towards the limit).
        voice: Who speaks; cast by the character asked for and vary it. bill: old man, wise and grumpy.
            george: mature British storyteller (m). callum: gravelly trickster (m). harry: rough, fierce young
            man, made for yelling. charlie: hyped young Aussie bloke. liam: energetic young man. lily: velvety,
            theatrical woman, great at whispers. matilda: warm middle-aged woman. jessica: bright, playful,
            giggly young woman. river: relaxed, neutral.
        dialogue: A skit between voices instead of one line: 2-8 turns, each with its own voice and script
            (tags work per turn), sent as one voice message; leave script and voice out. The limit counts
            every turn.
    """
    deps = ctx.deps
    turns = [(t["voice"], t["script"].strip()) for t in dialogue] if dialogue else [(voice, script.strip())]
    whole = "\n".join(text for _, text in turns)
    characters = sum(len(text) for _, text in turns)
    limit = deps.settings.speech.max_chars
    try:
        check_budget(deps)
        if dialogue is not None and not 2 <= len(turns) <= MAX_TURNS:
            raise Refused(f"A dialogue takes 2 to {MAX_TURNS} turns; for one voice, use script.")
        if not all(text for _, text in turns):
            raise Refused(
                "Nothing to say: write the script." if not dialogue else "Every turn needs a script."
            )
        if characters > limit:
            raise Refused(
                f"Too long for a voice message ({characters} characters, at most {limit} with tags)."
            )
        prices = await rates(deps)
        models = voicing(deps.settings.speech, prices.pin, whole)
        if dialogue and models:  # turns are joined as recorded, so they share one audio format
            audio = deps.settings.speech.model(models[0]).audio
            models = tuple(m for m in models if deps.settings.speech.model(m).audio == audio)
        if not models:
            raise Refused("No voice model can act that line out; try it without the sound effects.")
        models = await affordable(ctx, prices, models, characters)
    except Refused as refused:
        return str(refused)
    speeches: list[Speech] = []
    try:
        for who, text in turns:
            speeches.append(await _perform(deps, prices, text, who, models))
    except OpenRouterError as error:
        await settle(
            deps, Pending(calls=list[Billed](speeches)), delivered=False
        )  # turns already voiced are absorbed
        what = "timed out" if error.status == 0 else f"failed ({error.status})"
        return f"The voice message {what}; nothing was charged for it."
    except BaseException:
        await settle(deps, Pending(calls=list[Billed](speeches)), delivered=False)
        raise
    pending = Pending(calls=list(speeches), fees=["voice"] if deps.funding is Funding.SPONSORED else [])
    try:
        ogg = await effects.speech(deps.workers, joined([s.data for s in speeches]), bitrate="48k")
    except EffectError as error:
        await settle(deps, pending, delivered=False)
        return f"The voice message failed ({error.reason}); nothing was charged for it."
    except BaseException:  # cancelled while converting: the call is absorbed, never lost
        await settle(deps, pending, delivered=False)
        raise
    spoken = whole if not dialogue else "\n".join(f"{who}: {text}" for who, text in turns)
    made = Made("voice", ogg, "derp.ogg", "voice message", pending, spoken=Spoken(spoken, speeches[0].model))
    return await emit(deps, made, final=final)
