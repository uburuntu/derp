"""Image generation and editing, paid from Derpcoins.

Images need paid funding (the free daily Derpcoins don't cover one). A single
image goes straight out when it fits the run's hold, unless it costs more than
the approval line, in which case the requester gets a Run / Cancel prompt.
"""

from typing import Literal

from aiogram.exceptions import TelegramAPIError
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset
from sqlalchemy import select

from derp.agent.approvals import ask, needs_approval
from derp.agent.deps import RunDeps
from derp.agent.images import ImageJob, make_image
from derp.agent.runs import totals
from derp.agent.tools.sources import Refused, avatar_source, chat_photo_source, photo_owner
from derp.agent.tools.telegram import unconfirmed
from derp.agent.toolsets import Family
from derp.history.models import Message
from derp.inference.api import OpenRouterError
from derp.inference.roles import ModelRole
from derp.money.funding import PAID_SOURCES, Funding, run_cost, run_fees
from derp.money.wallets import balance
from derp.telegram.policy import AMBIGUOUS

toolset = FunctionToolset[RunDeps]()


async def spent_credits(ctx: RunContext[RunDeps]) -> int:
    """Credits the run has used so far: side calls in the ledger, its own model turns and its fees."""
    deps = ctx.deps
    async with deps.db.session() as session:
        side = await run_cost(session, deps.run_id)
        fees = await run_fees(session, deps.run_id)
    turns = totals(ctx.messages[deps.history_start :]).cost_usd
    return deps.settings.credits_for_usd(float(side + turns)) + fees


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
) -> str:
    """Draw one image, or edit an existing one, and send it to the chat.

    Args:
        prompt: What to draw, or what to change, in detail (English works best).
        edit: Edit the image in the current message or the message it replies to.
        edit_message_id: Edit the image of this stored message instead (e.g. one you just sent).
        profile_photo: Edit a profile photo: edit_message_id's sender, else the person replied to, else the asker.
        chat_photo: Edit this chat's photo.
        person: Edit this member's profile photo (name or @username), e.g. to draw them.
        aspect_ratio: Shape of the picture.
    """
    deps = ctx.deps
    if deps.tool_outputs_left <= 0:
        return "No more outputs fit in this answer."
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
        prompt_message = await ask(
            deps.db, deps.bot, deps.settings, deps.target,
            kind="image", args=job.as_args(), price=price, user_id=deps.user_id, run_id=deps.run_id,
            locale=deps.locale,
        )  # fmt: skip
        deps.delivered.append((prompt_message.message_id, "other"))
        return 'They were asked to confirm the price with a button; the image comes after they press it. End your turn with just ".".'
    if price > deps.funds.credits - await spent_credits(ctx):
        return f"Not enough Derpcoins left in this answer for an image (needs about {price}). Tell them /buy gets more."
    try:
        sent = await make_image(
            db=deps.db, api=deps.api, bot=deps.bot, gateway=deps.gateway,
            pin=await deps.pins.get(ModelRole.IMAGE), target=deps.target, job=job,
            run_id=deps.run_id, funding=deps.funding.value, user_id=deps.user_id,
        )  # fmt: skip
    except OpenRouterError as error:
        return f"The image failed ({error.status}); nothing was charged for it."
    except AMBIGUOUS:
        return unconfirmed("image")
    except TelegramAPIError:
        return "Telegram refused the image; nothing was charged for it."
    deps.delivered.append((sent.message_id, "image"))
    return f'Sent the image as #{sent.message_id}. Add at most a short line, or end with just ".".'


family = Family("images", toolset)
