"""Image generation and editing, paid from Derpcoins.

Images need paid funding (the free daily Derpcoins don't cover one). An image
goes straight out (or is kept for a next step) when it fits the run's hold,
unless it costs more than the approval line: then the requester gets a Make it /
Cancel prompt and the run pauses on the call; Make it resumes the run there, so
a chain (caption it, make it a sticker, cut it out) carries on from the image.

On a run a contributed key pays whose provider draws (``settings.keys.images``), the key draws it with
its provider's image model instead: no Derpcoins and no approval (the key's monthly cap is its owner's
limit), only while what is left of the cap covers one. Only such a key draws animations (``animate``): a
sprite sheet looped into a silent MP4, which Telegram shows as a GIF.
"""

from dataclasses import replace
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field
from pydantic_ai import ApprovalRequired, RunContext
from pydantic_ai.toolsets import FunctionToolset

from derp.agent.approvals import ask, needs_approval
from derp.agent.deps import RunDeps
from derp.agent.images import SHEET, ImageJob, Portrait, draw, filename
from derp.agent.media import referenced_media
from derp.agent.outputs import Made, Pending, Refused, afford, check_budget, deliver, emit, key_left, settle
from derp.agent.tools.sources import (
    CHAT_PHOTO,
    MESSAGE,
    THIS,
    Final,
    author_of,
    chat_photo_source,
    kept_ref,
    kept_source,
    resolve_person,
)
from derp.agent.toolsets import Family
from derp.inference.api import OpenRouterError
from derp.inference.roles import ModelRole
from derp.keys.routing import key_pin
from derp.money.funding import PAID_SOURCES, Funding
from derp.money.wallets import balance
from derp.telegram.media import MediaUnavailableError, profile_photo_bytes
from derp.tools import effects
from derp.tools.workers import EffectError

toolset = FunctionToolset[RunDeps]()
IMAGES = ("photo", "document", "sticker")  # kept results an edit can start from


@toolset.tool(sequential=True)  # the budget check and the send must not interleave
async def generate_image(
    ctx: RunContext[RunDeps],
    prompt: str,
    images: Annotated[list[str], Field(max_length=4)] | None = None,
    aspect_ratio: Literal["1:1", "16:9", "9:16", "4:3", "3:4"] = "1:1",
    animate: bool = False,
    final: Final = True,
) -> str:
    """Draw a picture, or change existing ones, and send it.

    Args:
        prompt: What to draw or change, in detail (English works best). For people drawn from their profile
            photos, say who is who ("Anya as the rogue").
        images: Pictures to start from, up to 4, named as sources are: "#id" (a message's image, else its author's
            profile photo), "r1", a name, @username or "me" (their profile photo: the likeness stays theirs),
            "chat".
        animate: A short looping GIF (square) instead of a still; describe the motion.
    """
    deps = ctx.deps
    refs = list(dict.fromkeys(ref.strip() for ref in images or () if ref.strip()))
    kept = [ref for ref in refs if kept_ref(ref)]
    try:
        if len(kept) > 1:
            raise Refused("Start from one kept result at a time.")
        check_budget(deps, using=kept_ref(kept[0]) if kept else None)
        source = kept_source(deps, kept_ref(kept[0]) or "", IMAGES) if kept else None
    except Refused as refused:
        return str(refused)
    price = deps.settings.credits_for_usd(deps.settings.image_price_usd)
    key = deps.funds.key
    drawn_by = deps.settings.keys.images.get(key.provider) if key is not None else None
    if animate and drawn_by is None:
        return "Animations are drawn only in chats running on someone's ChatGPT key; offer a still picture."
    if drawn_by is None and (deps.funding is not Funding.PAID or deps.funds.wallet_id is None):
        return "Images need Derpcoins (the free daily ones don't cover one). Tell them /buy gets some."
    if ctx.tool_call_approved:  # they pressed Make it: exactly the job the prompt priced
        job = ImageJob.from_args(ctx.tool_call_metadata or {})
    else:
        try:
            job = await _job(deps, prompt, [ref for ref in refs if ref not in kept], aspect_ratio, animate)
        except Refused as refused:
            return str(refused)
        if drawn_by is None and deps.funds.wallet_id is not None:
            async with deps.db.session() as session:
                rest = await balance(session, deps.funds.wallet_id, sources=PAID_SOURCES)
        else:
            rest = 0
        if drawn_by is None and needs_approval(deps.settings, price, deps.funds.credits + rest):
            if source is not None:
                return (
                    "This image costs enough that they must confirm the price first, so it can't start from a "
                    "kept result: draw it without it, or start from a posted image by its id."
                )
            prompt_card = lambda: ask(
                deps.db, deps.bot, deps.settings, deps.target,
                kind="image", args=job.as_args(), price=price, user_id=deps.user_id, run_id=deps.run_id,
                call=ctx.tool_call_id or "", locale=deps.locale,
            )  # fmt: skip
            try:
                await deliver(deps, "other", "price prompt", prompt_card, history=False)
            except Refused as refused:
                return str(refused)
            raise ApprovalRequired  # the run pauses here; Make it resumes it from this call
    if key is not None and drawn_by is not None:
        left = await key_left(ctx, next_turn=True)
        if left is not None and left < Decimal(str(deps.settings.keys.image_price_usd)):
            return "What's left of this key's monthly cap doesn't cover an image now; say so."
        pin, sub_key = key_pin(key.provider, drawn_by), key.sub_key
    else:
        try:  # an approved image was agreed to at this price: its resumed run is funded for it alone
            await afford(ctx, price, next_turn=not ctx.tool_call_approved)
        except Refused as refused:
            return str(refused)
        pin, sub_key = await deps.pins.get(ModelRole.IMAGE), None
    extra = [source.data] if source is not None else []
    try:
        image = await draw(
            db=deps.db, api=deps.api, bot=deps.bot, gateway=deps.gateway, pin=pin, chat_id=deps.chat.id, job=job,
            extra=extra, api_key=sub_key,
        )  # fmt: skip
    except OpenRouterError as error:
        if error.refused:
            who = error.provider or "The image model"
            return f"{who} wouldn't draw that one (its content rules); nothing was charged. Say so: they can rephrase."
        return f"The image failed ({error.status}); nothing was charged for it."
    except MediaUnavailableError as unavailable:
        return f"A reference picture can't be used ({unavailable.reason}); nothing was charged."
    if key is not None and drawn_by is not None:
        estimate = Decimal(
            str(deps.settings.keys.image_price_usd)
        )  # when the provider's charge wasn't reported
        upstream = image.upstream_usd if image.upstream_usd is not None else estimate
        image = replace(
            image, key_id=key.key_id, upstream_usd=upstream
        )  # the key's from here on, sent or not
    if job.animated:
        try:
            clip = await effects.animate(deps.workers, image.data, *SHEET)
        except EffectError as failed:
            await settle(deps, Pending(calls=[image]), delivered=False)
            return f"The animation couldn't be put together ({failed.reason})."
        made = Made("animation", clip, "derp.mp4", "animation", Pending(calls=[image]))
    else:
        made = Made("photo", image.data, filename(image), "image", Pending(calls=[image]))
    try:  # the image is charged with the run only once it (or a result made from it) reached Telegram
        result = await emit(deps, made, final=final, source=source.kept if source else None)
    except Refused as refused:
        return str(refused)
    if job.faceless:
        result += f" No profile photo of {', '.join(job.faceless)}: drawn from the description."
    return result


async def _job(deps: RunDeps, prompt: str, refs: list[str], aspect_ratio: str, animate: bool) -> ImageJob:
    """What to draw, its references resolved now; ``Refused`` when one can't be.

    "#id" edits that message's image ("this": the images the run looks at), or draws its author when it has
    none; "chat" edits the chat photo; anything else names a person. People are drawn from their profile
    photos, or from the description when they have none.
    """
    references: list[int] = []
    people: list[Portrait] = []
    for ref in refs:
        if found := MESSAGE.fullmatch(ref):
            message_id = int(found.group(1))
            async with deps.db.session() as session:
                pictured = await referenced_media(session, deps.chat.id, message_id, None)
            if pictured:
                references.append(message_id)
            else:
                people.append(await author_of(deps, message_id))
        elif ref.lower() in THIS:
            references.extend(deps.seen)
        elif ref.lower() in CHAT_PHOTO:
            await chat_photo_source(deps)  # no photo fails early
        else:
            people.append(await resolve_person(deps, ref))
    portraits: list[Portrait] = []
    faceless: list[str] = []
    for member in dict.fromkeys(people):
        if member.user_id in {p.user_id for p in portraits}:
            continue
        try:
            await profile_photo_bytes(deps.bot, deps.gateway, member.user_id)  # warms the cache for draw
            portraits.append(member)
        except MediaUnavailableError:
            faceless.append(member.name or "someone")
    chat_photo = any(ref.lower() in CHAT_PHOTO for ref in refs)
    return ImageJob(
        prompt[:2000],
        aspect_ratio,
        tuple(dict.fromkeys(references)),
        tuple(portraits),
        tuple(dict.fromkeys(faceless)),
        chat_photo,
        animate,
    )


family = Family("images", toolset)
