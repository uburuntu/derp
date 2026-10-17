"""Self-service: balances, buying, settings and feedback, without leaving the conversation.

Balances are private: in groups they go to the asker by DM and the model only
learns that they were sent. Purchase buttons are real Stars invoices (or the
pack menu). Settings changes follow /settings rules — chat settings need an
admin, re-checked here (a time zone nobody set yet is anyone's to set).
Destructive or consenting changes only post a button: turning context off and
shortening retention delete history, and free models need the person's (or in
groups an admin's) own press on the notice that their providers may store
prompts.
"""

from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from derp.agent.deps import RunDeps
from derp.agent.tools.telegram import delivered, unconfirmed
from derp.agent.toolsets import Family
from derp.billing.menu import link_menu, personal_menu, send_pack_invoice
from derp.billing.patrons import lately_line
from derp.history.models import Chat, User
from derp.history.render import describe_time
from derp.memory.notes import is_admin
from derp.money.command import credits_card
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
async def wallet_status(ctx: RunContext[RunDeps]) -> str:
    """Check the asker's balance: Derpcoins bought, Derp+, free today and this chat's; sent to them privately."""
    deps = ctx.deps
    text = await credits_card(deps.db, deps.settings, deps.bot, deps.chat, deps.user_id, deps.locale)
    if deps.chat.type == "private":
        return text + "\n\n(Shown only to them in this private chat; summarize it.)"
    patrons = await lately_line(deps.db, deps.chat.id, "en")
    aside = f" (Bring up only if asked — {patrons})" if patrons else ""
    if await send_privately(deps.bot, deps.user_id, text):
        return "Sent their balances to them privately. Say so; never guess or repeat amounts here." + aside
    return (
        "They haven't opened a private chat with Derp, so balances can't be sent; ask them to DM Derp /credits."
        + aside
    )


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
    packs = ", ".join(f"{p.stars}⭐ → {p.credits:,}" for p in settings.packs)
    if stars is not None:
        pack = next((p for p in settings.packs if p.stars == stars), None)
        if pack is None:
            return f"No pack costs {stars}⭐. Packs: {packs}."
        chat_id, thread = (deps.user_id, None) if personal_in_group else (deps.chat.id, deps.thread_id)
        try:
            sent = await send_pack_invoice(
                deps.bot, chat_id, thread, deps.locale, pack, kind=kind, owner_id=owner
            )
        except AMBIGUOUS:
            return unconfirmed("invoice")
        except TelegramAPIError as error:
            if personal_in_group and isinstance(error, TelegramForbiddenError | TelegramBadRequest):
                return "They haven't opened a private chat with Derp; ask them to DM Derp /buy."
            return f"Telegram refused the invoice ({type(error).__name__})."
        if personal_in_group:
            return f"Sent them the invoice for {pack.credits:,} Derpcoins in their private chat with Derp."
        await delivered(deps, sent, "other")
        who = "this chat's wallet (anyone here can pay)" if kind == "chat" else "the asker"
        return f"Posted the invoice for {pack.credits:,} Derpcoins for {who}."
    if personal_in_group:
        text, markup = await personal_menu(deps.db, settings, deps.user_id, deps.locale)
        if await send_privately(deps.bot, deps.user_id, text, reply_markup=markup):
            return "Sent the pack menu to them privately."
        return "They haven't opened a private chat with Derp; ask them to DM Derp /buy."
    try:
        if group:  # invoice links: paying needs no public invoice per pack
            text, markup = await link_menu(
                deps.db,
                deps.bot,
                settings,
                deps.locale,
                kind="chat",
                owner_id=owner,
                thread_id=deps.thread_id,
            )
        else:
            text, markup = await personal_menu(deps.db, settings, deps.user_id, deps.locale)
        sent_menu = await send_markdown(deps.bot, deps.target, text, reply_markup=markup)
    except AMBIGUOUS:
        return unconfirmed("menu")
    except TelegramAPIError as error:
        return f"Telegram refused the menu ({type(error).__name__})."
    for message in sent_menu:
        await delivered(deps, message, "other")
    return f"Posted the pack menu ({packs})."


@toolset.tool
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
                    session, deps.bot, deps.settings, chat, None, deps.locale, "ambient_off"
                )
            await send_markdown(deps.bot, deps.target, text, reply_markup=markup)
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
                        session, deps.bot, deps.settings, chat, None, deps.locale, f"shorten:{arg}"
                    )
                await send_markdown(deps.bot, deps.target, text, reply_markup=markup)
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
            await send_markdown(deps.bot, Target(deps.chat.id, deps.thread_id), text, reply_markup=markup)
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


async def _user(deps: RunDeps) -> User | None:
    async with deps.db.session() as session:
        return await session.get(User, deps.user_id)


@toolset.tool
async def leave_feedback(
    ctx: RunContext[RunDeps], text: str, kind: Literal["bug", "idea", "praise", "other"] = "other"
) -> str:
    """Send feedback to Derp's makers: report a bug, suggest a feature, pass on praise.

    Use when someone asks to report or suggest something, in their words (lightly cleaned up).

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
