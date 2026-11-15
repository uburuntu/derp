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
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from pydantic_ai import RunContext

from derp.agent.deps import RunDeps
from derp.agent.outputs import Made, Pending, Refused, check_budget, emit, settle
from derp.agent.tools.effects import heavy_allowed
from derp.agent.tools.sources import FinalArg
from derp.config import SpeechModel, VoiceMessages
from derp.history.capture import Spoken
from derp.inference.api import OpenRouterError, Speech
from derp.inference.ledger import record_usage, simple_usage
from derp.inference.pins import snapshot_prices
from derp.inference.roles import ModelRole, RolePin
from derp.money.funding import Funding
from derp.tools import effects
from derp.tools.workers import EffectError

log = logging.getLogger("derp.voice")
# Stock voices that act well in Russian and English; the tool's docstring describes each by vibe.
Voice = Literal["callum", "harry", "bill", "charlie", "jessica", "lily"]
# Each voice's character, for a model that takes a delivery note rather than ElevenLabs' stock voices.
CHARACTERS: dict[str, str] = {
    "callum": "a gravelly trickster",
    "harry": "a rough, fierce man, made for yelling",
    "bill": "a grumpy old man",
    "charlie": "a hyped Australian bloke",
    "jessica": "a bright, playful, giggly woman",
    "lily": "a velvety, theatrical woman",
}
TAG = re.compile(r"\[([^\]]+)\]")
CYRILLIC = re.compile(r"[\u0400-\u04ff]")
# Tags a model without sound effects can still act from a delivery note: how the words are said. Any other
# tag ([laughs], [sighs], [fart], [applause]) is a sound, for a model that makes them.
DIRECTIONS = frozenset({
    "whispers", "whispering", "shouting", "shouts", "yelling", "yells", "screaming", "sarcastic", "deadpan",
    "excited", "angry", "furious", "sad", "calm", "serious", "dramatic", "dramatically", "cheerful", "happy",
    "nervous", "mischievous", "softly", "quietly", "loudly", "louder", "slowly", "quickly", "fast", "tired",
    "sleepy", "bored", "proud", "curious", "confused", "annoyed", "sing-song", "raspy", "grave", "weary",
    "storytelling", "normal", "menacing", "flirty", "smug", "robotic", "posh", "panicked",
})  # fmt: skip


def is_direction(tag: str) -> bool:
    tag = tag.strip().lower()
    return tag in DIRECTIONS or tag.endswith((" accent", " voice", " tone"))


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


async def speak(
    ctx: RunContext[RunDeps], script: str, voice: Voice = "callum", final: FinalArg = True
) -> str:
    """Act a short line out as a voice message. Only when someone asks to hear Derp: yell, whisper, sing, laugh,
    do an accent or a sound effect. Never as a plain reading of an answer.

    Write it to be performed: audio tags in square brackets go before the words they colour, e.g. [shouting],
    [whispers], [laughs harder], [sighs], [sarcastic], [crying], [excited], [sings], [strong French accent], and
    sound effects such as [applause], [explosion], [gunshot], [fart]. CAPITALS add emphasis, "..." a pause.
    Tags are acted, never read out; the words go in the chat's language.

    Args:
        script: The performance with its audio tags; a few sentences at most (tags count towards the limit).
        voice: callum: gravelly trickster (m). harry: rough and fierce, made for yelling (m). bill: grumpy old
            man (m). charlie: hyped Aussie bloke (m). jessica: bright, playful, giggly (f). lily: velvety and
            theatrical, great at whispers (f).
    """
    deps = ctx.deps
    text = script.strip()
    limit = deps.settings.speech.max_chars
    try:
        check_budget(deps)
        if not text:
            raise Refused("Nothing to say: write the script.")
        if len(text) > limit:
            raise Refused(
                f"Too long for a voice message ({len(text)} characters, at most {limit} with tags)."
            )
        prices = await rates(deps)
        models = voicing(deps.settings.speech, prices.pin, text)
        if not models:
            raise Refused("No voice model can act that line out; try it without the sound effects.")
        price = deps.settings.credits_for_usd(float(prices.most(len(text), models)))
        await heavy_allowed(ctx, price=price)
    except Refused as refused:
        return str(refused)
    try:
        speech = await _perform(deps, prices, text, voice, models)
    except OpenRouterError as error:
        what = "timed out" if error.status == 0 else f"failed ({error.status})"
        return f"The voice message {what}; nothing was charged for it."
    pending = Pending(calls=[speech], fees=["voice"] if deps.funding is Funding.SPONSORED else [])
    try:
        ogg = await effects.speech(deps.workers, speech.data, bitrate="48k")
    except EffectError as error:
        await settle(deps, pending, delivered=False)
        return f"The voice message failed ({error.reason}); nothing was charged for it."
    except BaseException:  # cancelled while converting: the call is absorbed, never lost
        await settle(deps, pending, delivered=False)
        raise
    made = Made("voice", ogg, "derp.ogg", "voice message", pending, spoken=Spoken(text, speech.model))
    return await emit(deps, made, final=final)
