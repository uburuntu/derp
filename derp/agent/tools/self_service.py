"""Self-service: balances, buying, settings and feedback, without leaving the conversation.

Personal balances are private: in groups they go to the asker by DM and the model only
learns that they were sent. Purchase buttons are real Stars invoices (or the
pack menu). Settings changes follow /settings rules — chat settings need an
admin, re-checked here (a time zone nobody set yet is anyone's to set).
Destructive or consenting changes only post a button: turning context off and
shortening retention delete history, and free models need the person's (or in
groups an admin's) own press on the notice that their providers may store
prompts.
"""

from datetime import UTC, date, datetime
from functools import partial
from typing import Any, Literal
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup
from aiogram.types import Message as TgMessage
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from derp.agent.deps import RunDeps
from derp.agent.outputs import Refused, deliver, unconfirmed
from derp.agent.tools.sources import replied_to
from derp.agent.toolsets import Family
from derp.billing.menu import link_menu, pack_label, personal_menu, send_pack_invoice
from derp.billing.patrons import lately
from derp.history.models import Chat, User
from derp.history.render import describe_time
from derp.keys.routing import candidates
from derp.memory.notes import is_admin
from derp.money import accounting
from derp.money.command import credits_card, receipt_text
from derp.money.free import consented, offer
from derp.ops import feedback
from derp.settings.panel import NOTES_MODES, apply_setting, chosen_days, render, shortens
from derp.telegram.policy import AMBIGUOUS
from derp.telegram.sender import Target, send_markdown, send_privately
from derp.tools.timezones import TimeZoneError, iana

toolset = FunctionToolset[RunDeps]()
Setting = Literal[
    "context",
    "retention_days",
    "time_zone",
    "notes",
    "free_models",
    "my_coins_in_groups",
    "name_me_when_i_chip_in",
]
PERSONAL: tuple[Setting, ...] = ("my_coins_in_groups", "name_me_when_i_chip_in")


@toolset.tool
async def chat_accounting(
    ctx: RunContext[RunDeps],
    view: Literal["balance", "usage", "receipt"],
    since: date | None = None,
    until: date | None = None,
    group_by: accounting.GroupBy = "total",
    member: str | None = None,
    model: str | None = None,
    call_type: str | None = None,
    funding: accounting.FundingSource | None = None,
    message: str | None = None,
) -> dict[str, Any] | str:
    """Look up this chat's shared wallet, actual usage and /info receipts. Give the figures in the chat.

    balance: spendable and held dc, enabled key models and recent patrons. Personal wallets in a group use wallet_status privately.
    usage: inclusive chat-local dates (default today), with optional filters and grouping.
    call_type: chat, image, stt, speech, media, digest, screening, decision, or a flat fee item.
    receipt: message id, #id or same-chat Telegram link; defaults to the message the person replied to.
    """
    deps = ctx.deps
    now = datetime.now(UTC)
    if view == "receipt":
        target = accounting.message_ref(deps.chat, message) if message else await replied_to(deps)
        if target is None:
            return "Reply to one of Derp's outputs or give its id or permalink from this chat."
        async with deps.db.session() as session:
            entries = await accounting.receipts(session, deps.chat.id, target)
        if not entries:
            return "There is no answer receipt for that message in this chat."
        return {
            "message": target,
            "receipts": [
                {
                    "version": "edit" if entry.revision else "original",
                    "receipt": receipt_text(entry, deps.locale),
                    "cached_input_tokens": entry.run.cache_read_tokens,
                    "provider_usd": str(entry.provider_usd),
                    "key_usd": str(entry.upstream_usd),
                    "tool_fees_dc": str(entry.fee_dc),
                }
                for entry in entries
            ],
        }
    async with deps.db.session() as session:
        if view == "balance":
            snapshot = await accounting.wallet_snapshot(session, deps.chat, deps.user_id, now)
            if deps.chat.type == "private":
                return snapshot
            return {
                **snapshot,
                "recent_patrons": await lately(session, deps.chat.id, now),
                "enabled_contributed_models": [
                    key.model for key in await candidates(session, deps.chat.id, now)
                ],
            }
        try:
            start, stop = accounting.date_range(since, until, now, deps.tz)
            return await accounting.usage(
                session,
                deps.chat,
                start,
                stop,
                group_by=group_by,
                member=member,
                model=model,
                call_type=call_type,
                funding=funding,
            )
        except ValueError as error:
            return str(error)


@toolset.tool
async def wallet_status(ctx: RunContext[RunDeps]) -> str:
    """Check the asker's balance: Derpcoins bought, Derp+, free today and this chat's; sent to them privately.

    In groups it also says who chipped in for the chat lately: mention them only when asked.
    """
    deps = ctx.deps
    text = await credits_card(deps.db, deps.settings, deps.bot, deps.chat, deps.user_id, deps.locale)
    if deps.chat.type == "private":
        return text + "\n\n(Shown only to them in this private chat; summarize it.)"
    if await send_privately(deps.bot, deps.user_id, text):
        result = "Sent their balances to them privately. Say so; never guess or repeat amounts here."
    else:
        result = "They haven't opened a private chat with Derp, so balances can't be sent; ask them to DM Derp /credits."
    async with deps.db.session() as session:
        patrons = await lately(session, deps.chat.id, datetime.now(UTC))
    return f"{result} Chipped in for this chat lately: {', '.join(patrons)}." if patrons else result


@toolset.tool(sequential=True)
async def purchase_button(ctx: RunContext[RunDeps], for_chat: bool = False, stars: int | None = None) -> str:
    """Buy Derpcoins: a Telegram Stars invoice (payment button) for a pack, or the pack menu.

    Args:
        for_chat: Buy for this group's shared wallet instead of the asker.
        stars: The pack's price in Stars (see the menu); omit to post the menu.
    """
    deps = ctx.deps
    settings = deps.settings
    group = deps.chat.type != "private"
    kind, owner = ("chat", deps.chat.id) if for_chat and group else ("user", deps.user_id)
    personal_in_group = kind == "user" and group  # a personal balance or invoice never shows in the group
    packs = ", ".join(pack_label(p) for p in settings.packs)
    if stars is not None:
        pack = next((p for p in settings.packs if p.stars == stars), None)
        if pack is None:
            return f"No pack costs {stars}⭐. Packs: {packs}."
        invoice = partial(
            send_pack_invoice,
            deps.bot,
            deps.locale,
            pack,
            kind=kind,
            owner_id=owner,
            thread_id=deps.target.thread_id,
        )
        if personal_in_group:  # it goes to their private chat: not an output of this one
            try:
                await invoice()
            except AMBIGUOUS:
                return unconfirmed("invoice")
            except TelegramForbiddenError, TelegramBadRequest:
                return "They haven't opened a private chat with Derp; ask them to DM Derp /buy."
            except TelegramAPIError as error:
                return f"Telegram refused the invoice ({type(error).__name__})."
            return f"Sent them the invoice for {pack.credits:,} Derpcoins in their private chat with Derp."
        try:
            await deliver(deps, "invoice", "invoice", invoice)
        except Refused as refused:
            return str(refused)
        who = "this chat's wallet (anyone here can pay)" if kind == "chat" else "the asker"
        return f"Posted the invoice for {pack.credits:,} Derpcoins for {who}."
    if personal_in_group:
        text, markup = await personal_menu(deps.db, settings, deps.user_id, deps.locale)
        if await send_privately(deps.bot, deps.user_id, text, reply_markup=markup):
            return "Sent the pack menu to them privately."
        return "They haven't opened a private chat with Derp; ask them to DM Derp /buy."

    async def post_menu() -> list[TgMessage]:
        if group:  # invoice links: paying needs no public invoice per pack
            text, markup = await link_menu(
                deps.db,
                deps.bot,
                settings,
                deps.locale,
                kind="chat",
                owner_id=owner,
                thread_id=deps.target.thread_id,
            )
        else:
            text, markup = await personal_menu(deps.db, settings, deps.user_id, deps.locale)
        return await send_markdown(deps.bot, deps.target, text, reply_markup=markup)

    try:
        await deliver(deps, "other", "menu", post_menu)
    except Refused as refused:
        return str(refused)
    return f"Posted the pack menu ({packs})."


@toolset.tool(sequential=True)  # may post a button card: an output
async def change_setting(ctx: RunContext[RunDeps], setting: Setting, value: str) -> str:
    """Change a setting: context, retention, time zone, notes, free models; the asker's coins and patron name.

    Set time_zone when someone says where they are or asks for their local time ("I'm in Moscow",
    "use Berlin time"): map the place to its IANA zone yourself.

    Args:
        setting: context (on/off), retention_days (days as offered in /settings, or default), time_zone
            (an IANA name like Europe/Moscow), notes (auto, members, admins, off), free_models (on/off),
            my_coins_in_groups (on/off), name_me_when_i_chip_in (on/off: named in the chat's thank-you post
            when they buy Derpcoins for a group, else "a mysterious patron").
        value: The new value.
    """
    deps = ctx.deps
    raw, value = value.strip(), value.strip().lower()
    group = deps.chat.type != "private"
    async with deps.db.session() as session:
        chat = await session.get(Chat, deps.chat.id) or deps.chat
    # In groups chat settings need an admin; a time zone nobody set yet is anyone's to set.
    chat_wide = group and setting not in PERSONAL and not (setting == "time_zone" and not chat.timezone)
    if chat_wide and not await is_admin(deps.bot, deps.chat, deps.user_id):
        return "Only this chat's admins can change that."
    options = deps.settings.retention.options
    match setting:
        case "context" if not group:
            return "Context is a group setting; private chats always keep what's said to Derp."
        case "context" if value == "off":
            async with deps.db.session() as session:
                text, markup = await render(
                    session, deps.bot, deps.settings, chat, None, deps.locale, "ambient_off", standalone=True
                )
            if refused := await _card(deps, deps.target, text, markup):
                return refused
            return "Posted a confirmation button: turning context off deletes stored history, so an admin must press it."
        case "context":
            action, arg = "ambient", "on"
        case "retention_days":
            arg = "0" if value == "default" else value
            if chosen_days(arg, deps.settings) is None:
                return f"Retention can be {', '.join(map(str, options))} days or default."
            if shortens(chat, deps.settings, arg):
                async with deps.db.session() as session:
                    text, markup = await render(
                        session,
                        deps.bot,
                        deps.settings,
                        chat,
                        None,
                        deps.locale,
                        f"shorten:{arg}",
                        standalone=True,
                    )
                if refused := await _card(deps, deps.target, text, markup):
                    return refused
                return (
                    "Posted a confirmation button: a shorter retention deletes older messages, so "
                    f"{'an admin' if group else 'they'} must press it."
                )
            action = "retention"
        case "time_zone":
            try:
                arg = iana(raw)
            except TimeZoneError as error:
                return f"{error}."
            action = "timezone"
        case "notes":
            if value not in NOTES_MODES:
                return "Notes mode is one of auto, members, admins, off."
            action, arg = "notes", value
        case "free_models" if value in ("on", "true", "yes"):
            if chat.free_models if group else consented(await _user(deps)):
                return "Free models are already on."
            text, markup = offer(deps.locale, group=group, asked=True)
            if refused := await _card(deps, Target(deps.chat.id, deps.target.thread_id), text, markup):
                return refused
            who = "an admin presses it" if group else "they press it themselves"
            return f"Posted the free-models notice (their providers may store prompts) with its button; on only once {who}."
        case "free_models":
            action, arg = "free", "off"
        case "my_coins_in_groups":
            action, arg = "spend", "on" if value in ("on", "true", "yes") else "off"
        case "name_me_when_i_chip_in":
            action, arg = "named", "on" if value in ("on", "true", "yes") else "off"
    await apply_setting(deps.db, deps.settings, deps.chat.id, deps.user_id, action, arg)
    if setting == "name_me_when_i_chip_in":
        return f"Done: they're {'named' if arg == 'on' else 'anonymous'} when they buy Derpcoins for a chat."
    if setting == "time_zone":
        deps.chat.timezone, deps.tz = arg, ZoneInfo(arg)  # later tools in this run use it
        return f"Done: this chat's time zone is now {arg} (local time {describe_time(datetime.now(UTC), deps.tz)})."
    return f"Done: {setting} is now {value}. /settings shows everything."


async def _card(deps: RunDeps, target: Target, text: str, markup: InlineKeyboardMarkup | None) -> str | None:
    """Post a button card as one of the run's outputs (UI copy, so not history); the refusal text if it failed."""
    try:
        await deliver(
            deps,
            "other",
            "button",
            lambda: send_markdown(deps.bot, target, text, reply_markup=markup),
            history=False,
        )
    except Refused as refused:
        return str(refused)
    return None


async def _user(deps: RunDeps) -> User | None:
    async with deps.db.session() as session:
        return await session.get(User, deps.user_id)


@toolset.tool
async def leave_feedback(
    ctx: RunContext[RunDeps], text: str, kind: Literal["bug", "idea", "praise", "other"] = "other"
) -> str:
    """Send feedback to Derp's makers: report a bug, suggest a feature, pass on praise.

    Use when someone asks to report or suggest something, in their words (lightly cleaned up); file it
    right away, without investigating first.

    Args:
        text: The feedback, self-contained.
        kind: What it is.
    """
    deps = ctx.deps
    async with deps.db.transaction() as session:
        await feedback.file(
            session,
            user_id=deps.user_id,
            chat_id=deps.chat.id,
            text=text,
            kind=kind,
            message_id=deps.trigger_message_id,
            run_id=deps.run_id,
        )
    return "Filed for the owners. Confirm in one short line."


family = Family("self_service", toolset, deferred=True)
