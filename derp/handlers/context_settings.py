"""Native Telegram onboarding, context visibility, and admin controls."""

from __future__ import annotations

from enum import StrEnum

import logfire
from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.filters.chat_member_updated import (
    JOIN_TRANSITION,
    ChatMemberUpdatedFilter,
)
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from derp.db import (
    DatabaseManager,
    acknowledge_context_notice,
    claim_member_notice,
    clear_history_scope,
    set_ambient_history,
    set_history_retention,
    tombstone_user_messages,
)
from derp.history.policy import CONTEXT_NOTICE_VERSION
from derp.models import Chat as ChatModel
from derp.observability import report_exception

router = Router(name="context_settings")


class ContextAction(StrEnum):
    """Compact typed callback actions for the context panel."""

    MENU = "menu"
    PRIVACY = "privacy"
    TOGGLE = "toggle"
    RETENTION = "retention"
    DELETE_MINE_CONFIRM = "delete_mine_confirm"
    DELETE_MINE = "delete_mine"
    CLEAR_CONFIRM = "clear_confirm"
    CLEAR = "clear"


class ContextCallback(CallbackData, prefix="ctx"):
    """Authenticated server-side context mutation request."""

    action: ContextAction
    value: int = 0


async def ambient_delivery_available(bot: Bot, chat_id: int) -> bool:
    """Return whether Telegram can deliver ordinary group messages to Derp."""
    me = await bot.get_me()
    if me.can_read_all_group_messages is True:
        return True
    try:
        member = await bot.get_chat_member(chat_id, me.id)
    except Exception as exc:
        report_exception(
            "context_self_membership_failed",
            exception=exc,
            level="warning",
            chat_id=chat_id,
        )
        return False
    return member.status in {"creator", "administrator"}


async def actor_can_manage(bot: Bot, message: Message, actor_id: int) -> bool:
    """Revalidate group administrators for every privileged mutation."""
    if message.chat.type == "private":
        return message.chat.id == actor_id
    try:
        administrators = await bot.get_chat_administrators(message.chat.id)
    except Exception as exc:
        report_exception(
            "context_admin_lookup_failed",
            exception=exc,
            level="warning",
            chat_id=message.chat.id,
        )
        return False
    return any(member.user.id == actor_id for member in administrators)


def build_context_panel(
    chat: ChatModel | None,
    *,
    ambient_available: bool,
    can_manage: bool,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build one context-aware help/settings surface without a command wall."""
    retention_days = chat.retention_days if chat else 30
    is_private = bool(chat and chat.type == "private")
    enabled = bool(chat and chat.ambient_history_enabled and ambient_available)
    if is_private:
        state = "On"
        context_line = f"History: On · {retention_days} days"
    elif ambient_available:
        state = "On" if enabled else "Off"
        context_line = f"Context: {state} · {retention_days} days"
    else:
        state = "Mentions only"
        context_line = "Context: Mentions only"

    text = (
        "<b>Derp</b>\n"
        "Ask naturally in private, or mention/reply to Derp in a group.\n\n"
        f"{context_line}\n"
        "Recent chat messages help with follow-ups. Members can inspect this "
        "state and remove their own stored messages."
    )
    rows = [
        [
            InlineKeyboardButton(
                text="Try Derp",
                switch_inline_query_current_chat="",
            ),
            InlineKeyboardButton(
                text=(
                    f"History: {retention_days}d" if is_private else f"Context: {state}"
                ),
                callback_data=ContextCallback(
                    action=(
                        ContextAction.PRIVACY
                        if is_private
                        else ContextAction.TOGGLE
                        if can_manage
                        else ContextAction.MENU
                    ),
                    value=0 if enabled else 1,
                ).pack(),
            ),
        ]
    ]
    if can_manage:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{days}d{' ✓' if retention_days == days else ''}",
                    callback_data=ContextCallback(
                        action=ContextAction.RETENTION,
                        value=days,
                    ).pack(),
                )
                for days in (7, 30, 90)
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="Privacy & history",
                callback_data=ContextCallback(action=ContextAction.PRIVACY).pack(),
            ),
            InlineKeyboardButton(
                text="Settings",
                callback_data=ContextCallback(action=ContextAction.MENU).pack(),
            ),
        ]
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def build_privacy_panel(
    chat: ChatModel | None,
    *,
    can_manage: bool,
    thread_id: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build discoverable personal deletion and scoped admin cleanup controls."""
    retention_days = chat.retention_days if chat else 30
    scope_label = "topic" if thread_id is not None else "chat"
    rows = [
        [
            InlineKeyboardButton(
                text="Delete my messages",
                callback_data=ContextCallback(
                    action=ContextAction.DELETE_MINE_CONFIRM
                ).pack(),
            )
        ]
    ]
    if can_manage:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"Clear this {scope_label}",
                    callback_data=ContextCallback(
                        action=ContextAction.CLEAR_CONFIRM
                    ).pack(),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="Back",
                callback_data=ContextCallback(action=ContextAction.MENU).pack(),
            )
        ]
    )
    text = (
        "<b>Privacy and history</b>\n"
        f"Stored conversation context expires after {retention_days} days.\n\n"
        "You can remove your own stored messages at any time. "
        "Chat admins can clear the current chat or forum topic without removing "
        "separately approved shared facts."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def build_destructive_confirmation(
    *,
    action: ContextAction,
    label: str,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build a compact confirmation that states Telegram's deletion boundary."""
    text = (
        f"<b>{label}?</b>\n"
        "This removes Derp's stored source, media references, and derived history. "
        "It cannot remove Telegram's copy or text another member copied."
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Delete",
                    callback_data=ContextCallback(action=action).pack(),
                ),
                InlineKeyboardButton(
                    text="Cancel",
                    callback_data=ContextCallback(action=ContextAction.PRIVACY).pack(),
                ),
            ]
        ]
    )
    return text, markup


async def ensure_group_context_notice(
    message: Message,
    *,
    chat_model: ChatModel | None,
    db: DatabaseManager,
    bot: Bot,
) -> bool:
    """Show the one-time notice before enabling default ambient capture."""
    if (
        message.chat.type == "private"
        or not chat_model
        or chat_model.context_notice_version >= CONTEXT_NOTICE_VERSION
    ):
        return False
    available = await ambient_delivery_available(bot, message.chat.id)
    can_manage = bool(
        message.from_user and await actor_can_manage(bot, message, message.from_user.id)
    )
    chat_model.ambient_history_enabled = available
    text, markup = build_context_panel(
        chat_model,
        ambient_available=available,
        can_manage=can_manage,
    )
    await message.reply(text, reply_markup=markup)
    async with db.session() as session:
        await acknowledge_context_notice(
            session,
            chat_telegram_id=message.chat.id,
            ambient_enabled=available,
        )
    chat_model.context_notice_version = CONTEXT_NOTICE_VERSION
    return True


@router.my_chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_bot_joined(
    event: ChatMemberUpdated,
    db: DatabaseManager,
    bot: Bot,
    chat_model: ChatModel | None,
) -> None:
    """Send the compact notice when Derp joins a group, then enable capture."""
    if event.chat.type == "private" or not chat_model:
        return
    available = await ambient_delivery_available(bot, event.chat.id)
    chat_model.ambient_history_enabled = available
    text, markup = build_context_panel(
        chat_model,
        ambient_available=available,
        can_manage=True,
    )
    await bot.send_message(event.chat.id, text, reply_markup=markup)
    async with db.session() as session:
        await acknowledge_context_notice(
            session,
            chat_telegram_id=event.chat.id,
            ambient_enabled=available,
        )


@router.message(Command("help"))
@router.message(Command("settings"))
async def show_context_menu(
    message: Message,
    bot: Bot,
    chat_model: ChatModel | None,
) -> None:
    """Show the same discoverable surface for help and settings."""
    available = message.chat.type == "private" or await ambient_delivery_available(
        bot, message.chat.id
    )
    can_manage = bool(
        message.from_user and await actor_can_manage(bot, message, message.from_user.id)
    )
    text, markup = build_context_panel(
        chat_model,
        ambient_available=available,
        can_manage=can_manage,
    )
    await message.reply(text, reply_markup=markup)


@router.callback_query(ContextCallback.filter(F.action == ContextAction.MENU))
async def refresh_context_menu(
    query: CallbackQuery,
    bot: Bot,
    chat_model: ChatModel | None,
) -> None:
    """Refresh state without granting mutation rights."""
    if not isinstance(query.message, Message):
        return await query.answer()
    available = query.message.chat.type == "private" or (
        await ambient_delivery_available(bot, query.message.chat.id)
    )
    can_manage = await actor_can_manage(bot, query.message, query.from_user.id)
    text, markup = build_context_panel(
        chat_model,
        ambient_available=available,
        can_manage=can_manage,
    )
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(ContextCallback.filter(F.action == ContextAction.PRIVACY))
async def show_privacy_menu(
    query: CallbackQuery,
    bot: Bot,
    chat_model: ChatModel | None,
) -> None:
    """Show personal deletion and authorized scope cleanup actions."""
    if not isinstance(query.message, Message):
        return await query.answer()
    can_manage = await actor_can_manage(bot, query.message, query.from_user.id)
    text, markup = build_privacy_panel(
        chat_model,
        can_manage=can_manage,
        thread_id=query.message.message_thread_id,
    )
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(
    ContextCallback.filter(F.action == ContextAction.DELETE_MINE_CONFIRM)
)
async def confirm_delete_my_history(query: CallbackQuery) -> None:
    """Require an explicit destructive confirmation for broad personal deletion."""
    if not isinstance(query.message, Message):
        return await query.answer()
    text, markup = build_destructive_confirmation(
        action=ContextAction.DELETE_MINE,
        label="Delete your stored messages from this chat",
    )
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(ContextCallback.filter(F.action == ContextAction.DELETE_MINE))
async def delete_my_history(
    query: CallbackQuery,
    db: DatabaseManager,
    bot: Bot,
    chat_model: ChatModel | None,
) -> None:
    """Anonymize every retained message owned by the requesting member."""
    if not isinstance(query.message, Message):
        return await query.answer("History is unavailable", show_alert=True)
    async with db.session() as session:
        removed = await tombstone_user_messages(
            session,
            chat_telegram_id=query.message.chat.id,
            actor_telegram_id=query.from_user.id,
        )
    can_manage = await actor_can_manage(bot, query.message, query.from_user.id)
    text, markup = build_privacy_panel(
        chat_model,
        can_manage=can_manage,
        thread_id=query.message.message_thread_id,
    )
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer(f"Removed {removed} stored messages")


@router.callback_query(ContextCallback.filter(F.action == ContextAction.CLEAR_CONFIRM))
async def confirm_clear_history(query: CallbackQuery, bot: Bot) -> None:
    """Revalidate admin authority before showing the scope purge confirmation."""
    if not isinstance(query.message, Message):
        return await query.answer()
    if not await actor_can_manage(bot, query.message, query.from_user.id):
        return await query.answer("Only chat admins can clear history", show_alert=True)
    scope = "topic" if query.message.message_thread_id is not None else "chat"
    text, markup = build_destructive_confirmation(
        action=ContextAction.CLEAR,
        label=f"Clear this {scope}'s stored history",
    )
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(ContextCallback.filter(F.action == ContextAction.CLEAR))
async def clear_current_history(
    query: CallbackQuery,
    db: DatabaseManager,
    bot: Bot,
    chat_model: ChatModel | None,
) -> None:
    """Purge exactly the current chat/topic after live admin authorization."""
    if not isinstance(query.message, Message):
        return await query.answer("History is unavailable", show_alert=True)
    if not await actor_can_manage(bot, query.message, query.from_user.id):
        return await query.answer("Only chat admins can clear history", show_alert=True)
    async with db.session() as session:
        removed = await clear_history_scope(
            session,
            chat_telegram_id=query.message.chat.id,
            thread_id=query.message.message_thread_id,
        )
    text, markup = build_privacy_panel(
        chat_model,
        can_manage=True,
        thread_id=query.message.message_thread_id,
    )
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer(f"Cleared {removed} stored messages")


@router.callback_query(ContextCallback.filter(F.action == ContextAction.TOGGLE))
async def toggle_context(
    query: CallbackQuery,
    callback_data: ContextCallback,
    db: DatabaseManager,
    bot: Bot,
    chat_model: ChatModel | None,
) -> None:
    """Enable or disable ambient capture after live admin authorization."""
    if not isinstance(query.message, Message) or not chat_model:
        return await query.answer("Settings are unavailable", show_alert=True)
    if not await actor_can_manage(bot, query.message, query.from_user.id):
        return await query.answer("Only chat admins can change this", show_alert=True)
    enable = bool(callback_data.value)
    available = query.message.chat.type == "private" or (
        await ambient_delivery_available(bot, query.message.chat.id)
    )
    if enable and not available:
        return await query.answer(
            "Telegram currently delivers mentions only",
            show_alert=True,
        )
    async with db.session() as session:
        purged = await set_ambient_history(
            session,
            chat_telegram_id=query.message.chat.id,
            enabled=enable,
        )
    chat_model.ambient_history_enabled = enable
    text, markup = build_context_panel(
        chat_model,
        ambient_available=available,
        can_manage=True,
    )
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer(
        "Context enabled" if enable else f"Context disabled · removed {purged} messages"
    )
    logfire.info(
        "ambient_context_changed",
        chat_id=query.message.chat.id,
        enabled=enable,
        purged_messages=purged,
    )


@router.callback_query(ContextCallback.filter(F.action == ContextAction.RETENTION))
async def change_retention(
    query: CallbackQuery,
    callback_data: ContextCallback,
    db: DatabaseManager,
    bot: Bot,
    chat_model: ChatModel | None,
) -> None:
    """Apply an approved retention period after live admin authorization."""
    if not isinstance(query.message, Message) or not chat_model:
        return await query.answer("Settings are unavailable", show_alert=True)
    if not await actor_can_manage(bot, query.message, query.from_user.id):
        return await query.answer("Only chat admins can change this", show_alert=True)
    async with db.session() as session:
        await set_history_retention(
            session,
            chat_telegram_id=query.message.chat.id,
            retention_days=callback_data.value,
        )
    chat_model.retention_days = callback_data.value
    available = query.message.chat.type == "private" or (
        await ambient_delivery_available(bot, query.message.chat.id)
    )
    text, markup = build_context_panel(
        chat_model,
        ambient_available=available,
        can_manage=True,
    )
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer(f"Retention set to {callback_data.value} days")


@router.message(Command("forget"))
async def forget_replied_message(message: Message, db: DatabaseManager) -> None:
    """Precisely anonymize one replied-to message owned by the requester."""
    if not message.from_user or not message.reply_to_message:
        await message.reply("Reply /forget to one of your own messages.")
        return
    async with db.session() as session:
        removed = await tombstone_user_messages(
            session,
            chat_telegram_id=message.chat.id,
            actor_telegram_id=message.from_user.id,
            telegram_message_id=message.reply_to_message.message_id,
        )
    if not removed:
        await message.reply(
            "That message is not stored here or does not belong to you."
        )
        return
    await message.reply(
        "Removed Derp's stored copy. Telegram's message and text copied by others "
        "are unchanged."
    )


@router.message(F.new_chat_members)
async def disclose_context_to_new_members(
    message: Message,
    db: DatabaseManager,
    chat_model: ChatModel | None,
) -> None:
    """Coalesce a compact, rate-limited disclosure for newly joined members."""
    members = [member for member in message.new_chat_members or () if not member.is_bot]
    if not members or not chat_model or not chat_model.ambient_history_enabled:
        return
    async with db.session() as session:
        claimed = await claim_member_notice(
            session,
            chat_telegram_id=message.chat.id,
        )
    if claimed:
        await message.answer(
            "Derp uses recent messages in this chat for follow-ups. "
            "Open /settings to inspect context, retention, and deletion controls."
        )


__all__ = [
    "ContextAction",
    "ContextCallback",
    "actor_can_manage",
    "ambient_delivery_available",
    "build_context_panel",
    "build_destructive_confirmation",
    "build_privacy_panel",
    "ensure_group_context_notice",
    "router",
]
