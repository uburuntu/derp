"""Image generation and editing, paid from Derpcoins.

Images need paid funding (the free daily Derpcoins don't cover one). A single
image goes straight out when it fits the run's hold, unless it costs more than
the approval line, in which case the requester gets a Run / Cancel prompt.
"""

from typing import Literal

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset
from sqlalchemy import select

from derp.agent.approvals import ask, needs_approval
from derp.agent.deps import RunDeps
from derp.agent.images import ImageJob, draw, filename
from derp.agent.outputs import Made, Pending, Refused, check_budget, deliver, emit, pending_cost
from derp.agent.runs import totals
from derp.agent.tools.sources import avatar_source, chat_photo_source, kept_source, photo_owner
from derp.agent.toolsets import Family
from derp.history.models import Message
from derp.inference.api import OpenRouterError
from derp.inference.roles import ModelRole
from derp.money.funding import PAID_SOURCES, Funding, run_cost, run_fees
from derp.money.wallets import balance
from derp.telegram.media import sniff_image

toolset = FunctionToolset[RunDeps]()
IMAGES = ("photo", "document", "sticker")  # kept results an edit can start from


async def spent_credits(ctx: RunContext[RunDeps]) -> int:
    """Credits the run has used so far: side calls in the ledger, its own model turns and its fees."""
    deps = ctx.deps
    async with deps.db.session() as session:
        side = await run_cost(session, deps.run_id)
        fees = await run_fees(session, deps.run_id)
    turns = totals(ctx.messages[deps.history_start :]).cost_usd
    kept_usd, kept_fees = pending_cost(deps)  # carried by kept results, ledgered once they are posted
    paid_fees = kept_fees * deps.settings.effect_credits if deps.funding is Funding.PAID else 0
    return deps.settings.credits_for_usd(float(side + turns + kept_usd)) + fees + paid_fees


async def _references(deps: RunDeps, edit_message_id: int | None) -> tuple[int, ...]:
    if edit_message_id is not None:
        return (edit_message_id,)
    async with deps.db.session() as session:
        replied = await session.scalar(
            select(Message.reply_to_message_id).where(
                Message.chat_id == deps.chat.id, Message.message_id == deps.trigger_message_id
            )
        )
    return (deps.trigger_message_id, *([replied] if replied else []))


@toolset.tool(sequential=True)  # the budget check and the send must not interleave
async def generate_image(
    ctx: RunContext[RunDeps],
    prompt: str,
    edit: bool = False,
    edit_message_id: int | None = None,
    profile_photo: bool = False,
    chat_photo: bool = False,
    person: str | None = None,
    aspect_ratio: Literal["1:1", "16:9", "9:16", "4:3", "3:4"] = "1:1",
    from_result: str | None = None,
    final: bool = True,
) -> str:
    """Draw one image, or edit an existing one, and send it to the chat.

    For several steps (draw → cut out → sticker) pass final=False to every step but the last: each returns an
    id like r1 for the next tool's from_result, and only the end result is posted.

    Args:
        prompt: What to draw, or what to change, in detail (English works best).
        edit: Edit the image in the current message or the message it replies to.
        edit_message_id: Edit the image of this stored message instead (e.g. one you just sent).
        profile_photo: Edit a profile photo: edit_message_id's sender, else the person replied to, else the asker.
        chat_photo: Edit this chat's photo.
        person: Edit this member's profile photo (name or @username), e.g. to draw them.
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
    avatar_of: int | None = None
    try:
        if profile_photo or person:
            avatar_of = await photo_owner(deps, edit_message_id, person)
            await avatar_source(
                deps, edit_message_id, person
            )  # fails early (and warms the cache) without a photo
        if chat_photo:
            await chat_photo_source(deps)
    except Refused as refused:
        return str(refused)
    edits_message = (edit or edit_message_id is not None) and avatar_of is None
    references = await _references(deps, edit_message_id) if edits_message else ()
    job = ImageJob(prompt[:2000], aspect_ratio, references, avatar_of=avatar_of, chat_photo=chat_photo)
    async with deps.db.session() as session:
        rest = await balance(session, deps.funds.wallet_id, sources=PAID_SOURCES)
    if needs_approval(deps.settings, price, deps.funds.credits + rest):
        if source is not None or not final:
            return (
                "This image costs enough that they must confirm the price first, so it can't be part of a chain: "
                "draw it on its own (final, without from_result) and carry on from the posted image next time."
            )
        prompt_card = lambda: ask(
            deps.db, deps.bot, deps.settings, deps.target,
            kind="image", args=job.as_args(), price=price, user_id=deps.user_id, run_id=deps.run_id,
            locale=deps.locale,
        )  # fmt: skip
        try:
            await deliver(deps, "other", "price prompt", prompt_card, history=False)
        except Refused as refused:
            return str(refused)
        return 'They were asked to confirm the price with a button; the image comes after they press it. End your turn with just ".".'
    if price > deps.funds.credits - await spent_credits(ctx):
        return f"Not enough Derpcoins left in this answer for an image (needs about {price}). Tell them /buy gets more."
    extra = [(source.data, sniff_image(source.data) or "image/png")] if source is not None else []
    try:
        image = await draw(
            db=deps.db, api=deps.api, bot=deps.bot, gateway=deps.gateway,
            pin=await deps.pins.get(ModelRole.IMAGE), chat_id=deps.chat.id, job=job, extra=extra,
        )  # fmt: skip
    except OpenRouterError as error:
        return f"The image failed ({error.status}); nothing was charged for it."
    made = Made("photo", image.data, filename(image), "image", Pending(images=[image]))
    try:  # the image is charged with the run only once it (or a result made from it) reached Telegram
        return await emit(deps, made, final=final, source=source.kept if source else None)
    except Refused as refused:
        return str(refused)


family = Family("images", toolset)
