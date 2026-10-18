"""Image generation and editing, paid from Derpcoins.

Images need paid funding (the free daily Derpcoins don't cover one). An image
goes straight out (or is kept for a next step) when it fits the run's hold,
unless it costs more than the approval line: then the requester gets a Make it /
Cancel prompt and the run pauses on the call; Make it resumes the run there, so
a chain (caption it, make it a sticker, cut it out) carries on from the image.
"""

from typing import Annotated, Literal

from pydantic import Field
from pydantic_ai import ApprovalRequired, RunContext
from pydantic_ai.toolsets import FunctionToolset

from derp.agent.approvals import ask, needs_approval
from derp.agent.deps import RunDeps
from derp.agent.images import ImageJob, Portrait, draw, filename
from derp.agent.outputs import Made, Pending, Refused, check_budget, deliver, emit, hold_left
from derp.agent.tools.sources import (
    avatar_source,
    chat_photo_source,
    kept_source,
    photo_owner,
    resolve_person,
)
from derp.agent.toolsets import Family
from derp.inference.api import OpenRouterError
from derp.inference.roles import ModelRole
from derp.money.funding import PAID_SOURCES, Funding
from derp.money.wallets import balance
from derp.telegram.media import MediaUnavailableError, profile_photo_bytes

toolset = FunctionToolset[RunDeps]()
IMAGES = ("photo", "document", "sticker")  # kept results an edit can start from


@toolset.tool(sequential=True)  # the budget check and the send must not interleave
async def generate_image(
    ctx: RunContext[RunDeps],
    prompt: str,
    edit: bool = False,
    edit_message_id: int | None = None,
    profile_photo: bool = False,
    chat_photo: bool = False,
    people: Annotated[list[str], Field(max_length=4)] | None = None,
    aspect_ratio: Literal["1:1", "16:9", "9:16", "4:3", "3:4"] = "1:1",
    from_result: str | None = None,
    final: bool = True,
) -> str:
    """Draw one image, or edit an existing one, and send it to the chat.

    For several steps (draw → cut out → sticker) pass final=False to every step but the last: each returns an
    id like r1 for the next tool's from_result, and only the end result is posted.

    Args:
        prompt: What to draw, or what to change, in detail (English works best).
        edit: Edit the image this message is about: sent with it, replied to, or the asker's photo just before.
        edit_message_id: Edit the image of this stored message instead (e.g. one you just sent).
        profile_photo: Edit a profile photo: edit_message_id's sender, else the person replied to, else the asker.
        chat_photo: Edit this chat's photo.
        people: Draw these members from their profile photos, in this order (names or @usernames, "me" for
            the asker; up to 4), e.g. a group portrait. Say in the prompt who is who ("Anya as the rogue").
        aspect_ratio: Shape of the picture.
        from_result: Edit a result kept earlier in this answer (e.g. r1).
        final: False keeps the image for a next step instead of posting it.
    """
    deps = ctx.deps
    try:
        check_budget(deps, using=from_result)
        source = kept_source(deps, from_result, IMAGES) if from_result else None
    except Refused as refused:
        return str(refused)
    price = deps.settings.credits_for_usd(deps.settings.image_price_usd)
    if deps.funding is not Funding.PAID or deps.funds.wallet_id is None:
        return "Images need Derpcoins (the free daily ones don't cover one). Tell them /buy gets some."
    if ctx.tool_call_approved:  # they pressed Make it: exactly the job the prompt priced
        job = ImageJob.from_args(ctx.tool_call_metadata or {})
    else:
        try:
            job = await _job(
                deps, prompt, edit, edit_message_id, profile_photo, chat_photo, people or [], aspect_ratio
            )
        except Refused as refused:
            return str(refused)
        async with deps.db.session() as session:
            rest = await balance(session, deps.funds.wallet_id, sources=PAID_SOURCES)
        if needs_approval(deps.settings, price, deps.funds.credits + rest):
            if source is not None:
                return (
                    "This image costs enough that they must confirm the price first, so it can't start from a "
                    "kept result: draw it without from_result, or edit a posted image by its id."
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
    if (left := await hold_left(ctx)) is not None and price > left:
        return f"Not enough Derpcoins left in this answer for an image (needs about {price}). Tell them /buy gets more."
    extra = [source.data] if source is not None else []
    try:
        image = await draw(
            db=deps.db, api=deps.api, bot=deps.bot, gateway=deps.gateway,
            pin=await deps.pins.get(ModelRole.IMAGE), chat_id=deps.chat.id, job=job, extra=extra,
        )  # fmt: skip
    except OpenRouterError as error:
        return f"The image failed ({error.status}); nothing was charged for it."
    except MediaUnavailableError as unavailable:
        return f"A reference picture can't be used ({unavailable.reason}); nothing was charged."
    made = Made("photo", image.data, filename(image), "image", Pending(images=[image]))
    try:  # the image is charged with the run only once it (or a result made from it) reached Telegram
        result = await emit(deps, made, final=final, source=source.kept if source else None)
    except Refused as refused:
        return str(refused)
    if job.faceless:
        result += f" No profile photo of {', '.join(job.faceless)}: drawn from the description."
    return result


async def _job(
    deps: RunDeps,
    prompt: str,
    edit: bool,
    edit_message_id: int | None,
    profile_photo: bool,
    chat_photo: bool,
    people: list[str],
    aspect_ratio: str,
) -> ImageJob:
    """What to draw, its references resolved now; ``Refused`` when one can't be.

    Named people are drawn from their profile photos, or from the description when they have none;
    ``profile_photo`` alone edits one photo, which must exist.
    """
    portraits: list[Portrait] = []
    faceless: list[str] = []
    for name in people:
        member = await resolve_person(deps, name)
        if member.user_id in {p.user_id for p in portraits}:
            continue
        try:
            await profile_photo_bytes(deps.bot, deps.gateway, member.user_id)  # warms the cache for draw
            portraits.append(member)
        except MediaUnavailableError:
            faceless.append(member.name or name)
    owner_photo = profile_photo and not people  # edit_message_id then names whose photo, not an image
    if owner_photo:
        await avatar_source(deps, edit_message_id)  # no photo fails early (and warms the cache)
        portraits.append(await photo_owner(deps, edit_message_id))
    if chat_photo:
        await chat_photo_source(deps)
    edits_message = (edit or edit_message_id is not None) and not owner_photo
    references = ((edit_message_id,) if edit_message_id is not None else deps.seen) if edits_message else ()
    return ImageJob(
        prompt[:2000], aspect_ratio, references, tuple(portraits), tuple(dict.fromkeys(faceless)), chat_photo
    )


family = Family("images", toolset)
