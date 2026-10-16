"""Self-service: balances, buying, settings and feedback, without leaving the conversation.

Balances are private: in groups they go to the asker by DM and the model only
learns that they were sent. Purchase buttons are real Stars invoices (or the
pack menu). Settings changes follow /settings rules — chat settings need an
admin, re-checked here, and turning context off only posts a confirmation
button, because it deletes history.
"""

from typing import Literal

from aiogram.exceptions import TelegramAPIError
from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset

from derp.agent.deps import RunDeps
from derp.agent.tools.telegram import delivered
from derp.agent.toolsets import Family
from derp.billing.command import packs_keyboard, send_pack_invoice
from derp.locales import t
from derp.memory.notes import is_admin
from derp.money.command import credits_card
from derp.money.wallets import balance, wallet
from derp.ops import feedback
from derp.settings.panel import NOTES_MODES, RETENTION_CHOICES, apply_setting, render
from derp.telegram.sender import Target, send_markdown, send_privately

toolset = FunctionToolset[RunDeps]()
Setting = Literal["context", "retention_days", "notes", "free_models", "my_coins_in_groups"]


@toolset.tool
async def wallet_status(ctx: RunContext[RunDeps]) -> str:
    """Check the asker's balance: Derpcoins bought, Derp+, free today and this chat's; sent to them privately."""
    deps = ctx.deps
    text = await credits_card(deps.db, deps.settings, deps.bot, deps.chat, deps.user_id, deps.locale)
    if deps.chat.type == "private":
        return text + "\n\n(Shown only to them in this private chat; summarize it.)"
    if await send_privately(deps.bot, deps.user_id, text):
        return "Sent their balances to them privately. Say so; never guess or repeat amounts here."
    return "They haven't opened a private chat with Derp, so balances can't be sent; ask them to DM Derp /credits."


@toolset.tool(sequential=True)
async def purchase_button(ctx: RunContext[RunDeps], for_chat: bool = False, stars: int | None = None) -> str:
    """Buy Derpcoins: post a Telegram Stars invoice (payment button) for a pack, or the pack menu.

    Args:
        for_chat: Buy for this group's shared wallet instead of the asker.
        stars: The pack's price in Stars (see the menu); omit to post the menu.
    """
    deps = ctx.deps
    settings = deps.settings
    group = deps.chat.type != "private"
    kind, owner = ("chat", deps.chat.id) if for_chat and group else ("user", deps.user_id)
    packs = ", ".join(f"{p.stars}⭐ → {p.credits:,}" for p in settings.packs)
    if stars is not None:
        pack = next((p for p in settings.packs if p.stars == stars), None)
        if pack is None:
            return f"No pack costs {stars}⭐. Packs: {packs}."
        try:
            sent = await send_pack_invoice(
                deps.bot, deps.chat.id, deps.thread_id, deps.locale, pack, kind=kind, owner_id=owner
            )
        except TelegramAPIError as error:
            return f"Telegram refused the invoice ({type(error).__name__})."
        await delivered(deps, sent, "other")
        who = "this chat's wallet" if kind == "chat" else "the asker (only they can pay it)"
        return f"Posted the invoice for {pack.credits:,} Derpcoins for {who}."
    async with deps.db.transaction() as session:
        left = await balance(session, (await wallet(session, kind, owner)).id)
    menu_key = "billing.menu_chat" if kind == "chat" else "billing.menu"
    text = t(deps.locale, menu_key, balance=f"{left:,}")
    markup = packs_keyboard(settings, kind, deps.locale, plan=not group)
    if kind == "user" and group:  # a personal balance never shows in the group
        if await send_privately(deps.bot, deps.user_id, text, reply_markup=markup):
            return "Sent the pack menu to them privately."
        return "They haven't opened a private chat with Derp; ask them to DM Derp /buy."
    try:
        sent = await send_markdown(deps.bot, deps.target, text, reply_markup=markup)
    except TelegramAPIError as error:
        return f"Telegram refused the menu ({type(error).__name__})."
    for message in sent:
        await delivered(deps, message, "other")
    return f"Posted the pack menu ({packs})."


@toolset.tool
async def change_setting(ctx: RunContext[RunDeps], setting: Setting, value: str) -> str:
    """Change a setting (/settings): context, history retention, notes, free models, or the asker's coins in groups.

    Args:
        setting: context (on/off), retention_days (7, 30, 90, 365 or default), notes (auto, members,
            admins, off), free_models (on/off), my_coins_in_groups (on/off).
        value: The new value.
    """
    deps = ctx.deps
    value = value.strip().lower()
    chat_wide = setting != "my_coins_in_groups" and deps.chat.type != "private"
    if chat_wide and not await is_admin(deps.bot, deps.chat, deps.user_id):
        return "Only this chat's admins can change that."
    match setting:
        case "context" if deps.chat.type == "private":
            return "Context is a group setting; private chats always keep what's said to Derp."
        case "context" if value == "off":
            async with deps.db.session() as session:
                text, markup = await render(
                    session, deps.settings, deps.chat, None, deps.locale, "ambient_off"
                )
            await send_markdown(deps.bot, deps.target, text, reply_markup=markup)
            return "Posted a confirmation button: turning context off deletes stored history, so an admin must press it."
        case "context":
            action, arg = "ambient", "on"
        case "retention_days":
            if value != "default" and (not value.isdigit() or int(value) not in RETENTION_CHOICES):
                return "Retention can be 7, 30, 90, 365 days or default."
            action, arg = "retention", "0" if value == "default" else value
        case "notes":
            if value not in NOTES_MODES:
                return "Notes mode is one of auto, members, admins, off."
            action, arg = "notes", value
        case "free_models":
            action, arg = "free", "on" if value in ("on", "true", "yes") else "off"
        case "my_coins_in_groups":
            action, arg = "spend", "on" if value in ("on", "true", "yes") else "off"
    await apply_setting(deps.db, deps.chat.id, deps.user_id, action, arg)
    if setting == "free_models" and arg == "on" and deps.chat.type != "private":
        await send_markdown(
            deps.bot, Target(deps.chat.id), t(deps.chat.locale or deps.locale, "money.free_enabled")
        )
    return f"Done: {setting} is now {value}. /settings shows everything."


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
