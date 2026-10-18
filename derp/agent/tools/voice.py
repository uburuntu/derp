"""Voice messages: Derp acts a short line out (yelling, whispering, singing, accents, sound effects).

The model writes the performance with inline audio tags and picks one of a few
stock voices; the speech pin (``ModelRole.SPEECH``) voices it, the shared
worker pool turns the MP3 into OGG/Opus, and it goes out through
``derp.agent.outputs`` as a voice message whose script is stored as its
transcript. The provider call travels with the result and is ledgered against
the run only once Telegram accepted it (or a result made from it); otherwise
it is absorbed. Paid runs pay its characters; sponsored runs pay them from the
day's allowance and count it as one of the day's heavy effects.
"""

import logging
from typing import Literal

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from derp.agent.deps import RunDeps
from derp.agent.outputs import Made, Pending, Refused, check_budget, emit, settle
from derp.agent.tools.effects import PAID_AND_SPONSORED, heavy_allowed
from derp.agent.toolsets import Family
from derp.history.capture import Spoken
from derp.inference.api import OpenRouterAPI, OpenRouterError, Speech
from derp.inference.roles import ModelRole, RolePin
from derp.money.funding import Funding
from derp.tools import effects
from derp.tools.workers import EffectError

log = logging.getLogger("derp.voice")
toolset = FunctionToolset[RunDeps]()
# Stock voices that act well in Russian and English; the tool's docstring describes each by vibe.
type Voice = Literal["callum", "harry", "bill", "charlie", "jessica", "lily"]


async def _perform(api: OpenRouterAPI, pin: RolePin, script: str, voice: str) -> Speech:
    error = OpenRouterError(503, "no speech model pinned")
    for model in pin.models:
        try:
            return await api.speak(model, script, voice, zdr=pin.zdr)
        except OpenRouterError as failed:  # next model in the pin
            log.info("speech model failed: %s", failed.status)
            error = failed
    raise error


@toolset.tool(sequential=True)  # the budget check and the send must not interleave
async def speak(ctx: RunContext[RunDeps], script: str, voice: Voice = "callum", final: bool = True) -> str:
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
        final: False keeps the voice message for a next step (e.g. reverse_media) instead of posting it.
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
        pin = await deps.pins.get(ModelRole.SPEECH)
        price = max(
            deps.settings.credits_for_usd(deps.settings.speech.price_usd(model, len(text)))
            for model in pin.models
        )
        await heavy_allowed(ctx, price=price)
    except Refused as refused:
        return str(refused)
    try:
        speech = await _perform(deps.api, pin, text, voice)
    except OpenRouterError as error:
        return f"The voice message failed ({error.status}); nothing was charged for it."
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


family = Family("voice", toolset, tiers=PAID_AND_SPONSORED)
