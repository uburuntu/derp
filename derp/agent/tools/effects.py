"""Local media effects: memes, stickers, reversing, cutouts, and song and anime recognition.

The source is named by one ``source`` string (``sources.source_of``): a stored message of this chat, a
result kept earlier in the run, a member's profile photo or the chat photo; by default the current
message or the one it replies to. Work runs in the capped local worker pool (``derp.tools.workers``).

Image memes, stickers, mirrored pictures and reversed voice are free on paid and sponsored runs. Heavy
effects (anything that encodes video, and background removal) cost a small flat fee on paid runs and are
counted per person per day on sponsored ones (voice messages share that count: ``voice.py``). The fee is
a ``run_fees`` row added to the run's charge, written only after Telegram accepted the result (or one
made from it, see ``derp.agent.outputs`` for chains), so a failed send costs nothing. Quote stickers
(``quotes.py``) and voice lines (``voice.py``) are effects tools too, added to ``toolset`` by the
registry (``derp.agent.tools``).
"""

from datetime import UTC, datetime, time
from typing import Literal

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset
from sqlalchemy import func, select

from derp.agent.deps import RunDeps
from derp.agent.outputs import Made, Pending, Sendable, afford, check_budget, emit, pending_cost
from derp.agent.tools.sources import SOUND, VISUAL, Final, Refused, SourceArg, kept_ref, source_of
from derp.agent.toolsets import Family
from derp.money.funding import Funding
from derp.money.models import RunFee
from derp.tools import background, effects, lookups
from derp.tools.captions import Style
from derp.tools.workers import EffectError

toolset = FunctionToolset[RunDeps]()


async def heavy_allowed(ctx: RunContext[RunDeps], *, price: int | None = None) -> None:
    """Refuse a heavy effect the answer can't cover; ``Refused`` says why.

    ``price`` (Derpcoins) is what it costs in this answer (default: the flat fee on paid runs, nothing on
    sponsored ones) and must fit what the hold has left (``outputs.afford``, a next turn set aside);
    sponsored runs also count it against today's effects.
    """
    deps = ctx.deps
    paid = deps.funding is Funding.PAID
    await afford(
        ctx, price if price is not None else deps.settings.effect_credits if paid else 0, next_turn=True
    )
    if paid:
        return
    async with deps.db.session() as session:
        used = await session.scalar(
            select(func.count()).where(
                RunFee.user_id == deps.user_id,
                RunFee.funding == Funding.SPONSORED.value,
                RunFee.created_at >= datetime.combine(datetime.now(UTC).date(), time(), tzinfo=UTC),
            )
        )
    if (used or 0) + pending_cost(deps)[1] >= deps.settings.sponsored_effects_per_day:
        raise Refused(
            "Today's free heavy effects (video effects, cutouts, voice messages) are used up for them; "
            "image memes still work, and Derpcoins (/buy) lift the limit."
        )


@toolset.tool(sequential=True)
async def make_meme(
    ctx: RunContext[RunDeps],
    text: str,
    style: Literal["demotivator", "lobster", "panel"] = "demotivator",
    source: SourceArg = None,
    final: Final = True,
) -> str:
    """Caption a picture, GIF, sticker or video as a meme and send it.

    Args:
        text: The caption, in the chat's language; short and punchy.
        style: demotivator (black frame, serif caption below), lobster (white script over the bottom) or
            panel (black text on a white panel above).
    """
    deps = ctx.deps
    try:
        check_budget(deps, using=kept_ref(source))
        found = await source_of(deps, source)
        if found.image:
            made = Made(
                "photo", await effects.meme_image(deps.workers, found.data, text, style), "meme.jpg", "meme"
            )
        else:
            await heavy_allowed(ctx)
            clip = await effects.meme_video(
                deps.workers, found.data, text, _style(style), sound=found.type in ("video", "video_note")
            )
            kind: Sendable = "video" if found.type in ("video", "video_note") else "animation"
            made = Made(kind, clip, "meme.mp4", "meme", Pending(fees=["meme"]))
        return await emit(deps, made, final=final, source=found.kept)
    except Refused as refused:
        return str(refused)
    except EffectError as error:
        return f"The meme failed ({error.reason}); nothing was charged."


def _style(style: str) -> Style:
    return "lobster" if style == "lobster" else "panel" if style == "panel" else "demotivator"


@toolset.tool(sequential=True)
async def media_effect(
    ctx: RunContext[RunDeps],
    effect: Literal["sticker", "reverse", "cutout"],
    source: SourceArg = None,
    final: Final = True,
) -> str:
    """Make media into a sticker, play it backwards, or cut a picture's subject out, and send it.

    Args:
        effect: sticker (a static sticker from a picture, a cutout or a clip's frame), reverse (clips and
            voice play backwards, pictures mirror) or cutout (the subject on a transparent background, as a
            PNG file).
    """
    deps = ctx.deps
    try:
        check_budget(deps, using=kept_ref(source))
        match effect:
            case "sticker":
                found = await source_of(deps, source)
                picture = found.data if found.image else await effects.still(deps.workers, found.data)
                made = Made(
                    "sticker", await effects.sticker(deps.workers, picture), "sticker.webp", "sticker"
                )
            case "reverse":
                found = await source_of(deps, source, (*VISUAL, "voice", "audio"))
                made = await _reversed(ctx, found.data, found.type, found.image)
            case "cutout":
                if not background.available(deps.settings.background_model):
                    raise Refused("Cutouts aren't available here.")
                found = await source_of(deps, source)
                if not found.image:
                    raise Refused("That isn't a still picture.")
                await heavy_allowed(ctx)
                cutout = await effects.remove_background(
                    deps.workers, found.data, deps.settings.background_model
                )
                made = Made("document", cutout, "cutout.png", "file", Pending(fees=["cutout"]))
        return await emit(deps, made, final=final, source=found.kept)
    except Refused as refused:
        return str(refused)
    except EffectError as error:
        return f"That didn't work ({error.reason}); nothing was charged."


async def _reversed(ctx: RunContext[RunDeps], data: bytes, kind: str, image: bool) -> Made:
    """Pictures mirrored, voice and audio played backwards as voice, clips backwards (a heavy effect)."""
    deps = ctx.deps
    if image:
        return Made("photo", await effects.mirror(deps.workers, data), "mirrored.jpg", "result")
    if kind in ("voice", "audio"):
        return Made("voice", await effects.reverse(deps.workers, data, voice=True), "reversed.ogg", "result")
    await heavy_allowed(ctx)
    clip = await effects.reverse(deps.workers, data)
    sendable: Sendable = "video_note" if kind == "video_note" else "video" if kind == "video" else "animation"
    return Made(sendable, clip, "reversed.mp4", "result", Pending(fees=["reverse"]))


@toolset.tool
async def identify(ctx: RunContext[RunDeps], what: Literal["song", "anime"], source: SourceArg = None) -> str:
    """Recognize the song playing in a voice message, audio or video, or the anime (title, episode, moment) a
    screenshot, GIF or clip is from.

    Args:
        what: song or anime.
    """
    deps = ctx.deps
    try:
        if what == "song":
            if not (deps.settings.acrcloud_host and deps.settings.acrcloud_access_secret):
                return "Song recognition isn't set up here."
            found = await source_of(deps, source, SOUND)
            return await deps.lookups.identify_song(await effects.audio_sample(deps.workers, found.data))
        found = await source_of(deps, source, VISUAL)
        frame = found.data if found.image else await effects.still(deps.workers, found.data)
        return await deps.lookups.identify_anime(frame)
    except Refused as refused:
        return str(refused)
    except EffectError as error:
        return f"Couldn't read that file ({error.reason})."
    except lookups.LookupError as error:
        return f"Recognition is unavailable right now ({error.reason})."


PAID_AND_SPONSORED = frozenset({Funding.PAID, Funding.SPONSORED})
family = Family("effects", toolset, tiers=PAID_AND_SPONSORED, deferred=True)
