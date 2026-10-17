"""Local media effects: memes, reversing, background removal, song and anime recognition, profile photos.

The source is the media of a stored message of this chat — the one named, or
else the current message or the one it replies to — or a member's profile
photo. Work runs in the capped local worker pool (``derp.tools.workers``).

Image memes, mirrored pictures and reversed voice are free on paid and
sponsored runs. Heavy effects (anything that encodes video, and background
removal) cost a small flat fee on paid runs and are counted per person per day
on sponsored ones. The fee is a ``run_fees`` row added to the run's charge,
written only after Telegram accepted the result, so a failed send costs nothing.
"""

from datetime import UTC, datetime, time
from typing import Literal

from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile
from aiogram.types import Message as TgMessage
from pydantic_ai import BinaryContent, RunContext
from pydantic_ai.toolsets import FunctionToolset
from sqlalchemy import func, select

from derp.agent.deps import RunDeps
from derp.agent.tools.images import spent_credits
from derp.agent.tools.sources import (
    SOUND,
    VISUAL,
    Refused,
    Source,
    avatar_source,
    chat_photo_source,
    message_media,
)
from derp.agent.toolsets import Family
from derp.history.capture import record_outgoing
from derp.money.funding import Funding
from derp.money.models import RunFee
from derp.telegram.media import sniff_image
from derp.telegram.policy import AMBIGUOUS
from derp.tools import background, effects, lookups
from derp.tools.captions import Style
from derp.tools.workers import EffectError

toolset = FunctionToolset[RunDeps]()
background_tools = FunctionToolset[RunDeps]()
song_tools = FunctionToolset[RunDeps]()
profile_tools = FunctionToolset[RunDeps]()
Sendable = Literal["photo", "video", "animation", "video_note", "voice", "document"]


async def _visual(
    deps: RunDeps,
    message_id: int | None,
    profile_photo: bool,
    chat_photo: bool,
    person: str | None,
    types: tuple[str, ...] = VISUAL,
) -> Source:
    if chat_photo:
        return await chat_photo_source(deps)
    if profile_photo or person:
        return await avatar_source(deps, message_id, person)
    return await message_media(deps, message_id, types)


def _check_outputs(deps: RunDeps) -> None:
    if deps.tool_outputs_left <= 0:
        raise Refused("No more outputs fit in this answer.")


async def _heavy_allowed(ctx: RunContext[RunDeps]) -> None:
    deps = ctx.deps
    if deps.funding is Funding.PAID:
        if deps.settings.effect_credits > deps.funds.credits - await spent_credits(ctx):
            raise Refused(
                "Not enough Derpcoins left in this answer for that effect. Tell them /buy gets more."
            )
        return
    async with deps.db.session() as session:
        used = await session.scalar(
            select(func.count()).where(
                RunFee.user_id == deps.user_id,
                RunFee.funding == Funding.SPONSORED.value,
                RunFee.created_at >= datetime.combine(datetime.now(UTC).date(), time(), tzinfo=UTC),
            )
        )
    if (used or 0) >= deps.settings.sponsored_effects_per_day:
        raise Refused(
            "Today's free video effects and cutouts are used up for them; image memes still work, "
            "and Derpcoins (/buy) lift the limit."
        )


async def _send(
    deps: RunDeps, kind: Sendable, data: bytes, filename: str, *, effect: str | None = None
) -> int:
    """Send the result; heavy effects (``effect`` set) are ledgered only once Telegram accepted it."""
    bot, target, media = deps.bot, deps.target, BufferedInputFile(data, filename=filename)
    chat, thread, reply = target.chat_id, target.thread_id, target.reply_parameters
    sent: TgMessage
    match kind:
        case "photo":
            sent = await bot.send_photo(chat, media, message_thread_id=thread, reply_parameters=reply)
        case "video":
            sent = await bot.send_video(
                chat, media, message_thread_id=thread, reply_parameters=reply, supports_streaming=True
            )
        case "animation":
            sent = await bot.send_animation(chat, media, message_thread_id=thread, reply_parameters=reply)
        case "video_note":
            sent = await bot.send_video_note(chat, media, message_thread_id=thread, reply_parameters=reply)
        case "voice":
            sent = await bot.send_voice(chat, media, message_thread_id=thread, reply_parameters=reply)
        case "document":
            sent = await bot.send_document(chat, media, message_thread_id=thread, reply_parameters=reply)
    deps.delivered.append((sent.message_id, "image" if kind in ("photo", "document") else "other"))
    async with deps.db.transaction() as session:
        if effect is not None:
            paid = deps.funding is Funding.PAID
            session.add(
                RunFee(
                    run_id=deps.run_id,
                    chat_id=deps.chat.id,
                    user_id=deps.user_id,
                    item=effect,
                    funding=deps.funding.value,
                    credits=deps.settings.effect_credits if paid else 0,
                )
            )
        await record_outgoing(session, [sent], deps.bot.id)
    return sent.message_id


def _sent(message_id: int) -> str:
    return f'Sent it as #{message_id}. Add at most a short line, or end with just ".".'


@toolset.tool(sequential=True)
async def make_meme(
    ctx: RunContext[RunDeps],
    text: str,
    style: Literal["demotivator", "lobster", "panel"] = "demotivator",
    message_id: int | None = None,
    profile_photo: bool = False,
    chat_photo: bool = False,
    person: str | None = None,
) -> str:
    """Caption a picture, GIF, sticker or video as a meme and send it.

    Styles: demotivator (black frame, serif caption below), lobster (white Lobster-font text over the bottom),
    panel (white panel with black text above).

    Args:
        text: The caption, in the chat's language; keep it short and punchy.
        style: The meme layout.
        message_id: Use the media of this stored message (default: the current message or the one it replies to).
        profile_photo: Use the profile photo of message_id's sender (or of the person replied to, or the asker).
        chat_photo: Use this chat's photo.
        person: Use this member's profile photo (name or @username).
    """
    deps = ctx.deps
    try:
        _check_outputs(deps)
        source = await _visual(deps, message_id, profile_photo, chat_photo, person)
        if source.image:
            return _sent(
                await _send(deps, "photo", await effects.meme_image(source.data, text, style), "meme.jpg")
            )
        await _heavy_allowed(ctx)
        clip = await effects.meme_video(
            source.data, text, _style(style), sound=source.type in ("video", "video_note")
        )
        kind: Sendable = "video" if source.type in ("video", "video_note") else "animation"
        return _sent(await _send(deps, kind, clip, "meme.mp4", effect="meme"))
    except Refused as refused:
        return str(refused)
    except EffectError as error:
        return f"The meme failed ({error.reason}); nothing was charged."
    except AMBIGUOUS:
        return "Telegram didn't confirm the meme; it may have arrived. Don't send it again."
    except TelegramAPIError:
        return "Telegram refused the meme; nothing was charged."


def _style(style: str) -> Style:
    return "lobster" if style == "lobster" else "panel" if style == "panel" else "demotivator"


@toolset.tool(sequential=True)
async def reverse_media(
    ctx: RunContext[RunDeps],
    message_id: int | None = None,
    profile_photo: bool = False,
    chat_photo: bool = False,
    person: str | None = None,
) -> str:
    """Play a video, GIF, video message, voice or audio backwards (pictures get mirrored) and send it.

    Args:
        message_id: Use the media of this stored message (default: the current message or the one it replies to).
        profile_photo: Mirror the profile photo of message_id's sender (or of the person replied to, or the asker).
        chat_photo: Mirror this chat's photo.
        person: Use this member's profile photo (name or @username).
    """
    deps = ctx.deps
    try:
        _check_outputs(deps)
        source = await _visual(
            deps, message_id, profile_photo, chat_photo, person, (*VISUAL, "voice", "audio")
        )
        if source.image:
            return _sent(await _send(deps, "photo", await effects.mirror(source.data), "mirrored.jpg"))
        if source.type in ("voice", "audio"):
            return _sent(
                await _send(deps, "voice", await effects.reverse(source.data, voice=True), "reversed.ogg")
            )
        await _heavy_allowed(ctx)
        clip = await effects.reverse(source.data)
        kind: Sendable = (
            "video_note"
            if source.type == "video_note"
            else "video"
            if source.type == "video"
            else "animation"
        )
        return _sent(await _send(deps, kind, clip, "reversed.mp4", effect="reverse"))
    except Refused as refused:
        return str(refused)
    except EffectError as error:
        return f"Reversing failed ({error.reason}); nothing was charged."
    except AMBIGUOUS:
        return "Telegram didn't confirm the result; it may have arrived. Don't send it again."
    except TelegramAPIError:
        return "Telegram refused the result; nothing was charged."


@toolset.tool
async def identify_anime(ctx: RunContext[RunDeps], message_id: int | None = None) -> str:
    """Find which anime (title, episode, moment) a screenshot, GIF or clip is from, via trace.moe.

    Args:
        message_id: Use the media of this stored message (default: the current message or the one it replies to).
    """
    deps = ctx.deps
    try:
        source = await message_media(deps, message_id, VISUAL)
        frame = source.data if source.image else await effects.still(source.data)
        return await deps.lookups.identify_anime(frame)
    except Refused as refused:
        return str(refused)
    except EffectError as error:
        return f"Couldn't take a frame from that ({error.reason})."
    except lookups.LookupError as error:
        return f"trace.moe is unavailable right now ({error.reason})."


@song_tools.tool
async def identify_song(ctx: RunContext[RunDeps], message_id: int | None = None) -> str:
    """Recognize the song playing in a voice message, audio, video or video message (ACRCloud).

    Args:
        message_id: Use the media of this stored message (default: the current message or the one it replies to).
    """
    deps = ctx.deps
    try:
        source = await message_media(deps, message_id, SOUND)
        return await deps.lookups.identify_song(await effects.audio_sample(source.data))
    except Refused as refused:
        return str(refused)
    except EffectError as error:
        return f"Couldn't listen to that ({error.reason})."
    except lookups.LookupError as error:
        return f"Song recognition is unavailable right now ({error.reason})."


@background_tools.tool(sequential=True)
async def remove_background(
    ctx: RunContext[RunDeps],
    message_id: int | None = None,
    profile_photo: bool = False,
    chat_photo: bool = False,
    person: str | None = None,
) -> str:
    """Cut the subject out of a picture (transparent background) and send it as a PNG file.

    Args:
        message_id: Use the picture of this stored message (default: the current message or the one it replies to).
        profile_photo: Use the profile photo of message_id's sender (or of the person replied to, or the asker).
        chat_photo: Use this chat's photo.
        person: Use this member's profile photo (name or @username).
    """
    deps = ctx.deps
    try:
        _check_outputs(deps)
        source = await _visual(deps, message_id, profile_photo, chat_photo, person)
        if not source.image:
            raise Refused("That isn't a still picture.")
        await _heavy_allowed(ctx)
        cutout = await effects.remove_background(source.data, deps.settings.background_model)
        return _sent(await _send(deps, "document", cutout, "cutout.png", effect="cutout"))
    except Refused as refused:
        return str(refused)
    except EffectError as error:
        return f"Background removal failed ({error.reason}); nothing was charged."
    except AMBIGUOUS:
        return "Telegram didn't confirm the file; it may have arrived. Don't send it again."
    except TelegramAPIError:
        return "Telegram refused the file; nothing was charged."


@profile_tools.tool
async def profile_photo(
    ctx: RunContext[RunDeps],
    message_id: int | None = None,
    chat_photo: bool = False,
    person: str | None = None,
) -> list[str | BinaryContent] | str:
    """Look at someone's profile photo (message_id's sender, else the person replied to, else the asker) or the chat's.

    Only to see it: drawing and meme tools take profile_photo / chat_photo / person directly.

    Args:
        message_id: A stored message whose sender's profile photo to look at.
        chat_photo: Look at this chat's photo instead.
        person: Look at this member's profile photo (name or @username).
    """
    deps = ctx.deps
    if not deps.pin.vision:
        return "This model can't see images."
    try:
        source = await (chat_photo_source(deps) if chat_photo else avatar_source(deps, message_id, person))
    except Refused as refused:
        return str(refused)
    media_type = sniff_image(source.data)
    if media_type is None:
        return "That photo isn't a picture I can open."
    return [
        f"({'chat' if chat_photo else 'profile'} photo)",
        BinaryContent(source.data, media_type=media_type),
    ]


PAID_AND_SPONSORED = frozenset({Funding.PAID, Funding.SPONSORED})
family = Family("effects", toolset, tiers=PAID_AND_SPONSORED, deferred=True)
background_family = Family(
    "background",
    background_tools,
    tiers=PAID_AND_SPONSORED,
    deferred=True,
    available=lambda deps: background.available(deps.settings.background_model),
)
song_family = Family(
    "song",
    song_tools,
    tiers=PAID_AND_SPONSORED,
    deferred=True,
    available=lambda deps: bool(deps.settings.acrcloud_host and deps.settings.acrcloud_access_secret),
)
profile_family = Family("profile_photo", profile_tools, deferred=True)
