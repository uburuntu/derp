"""Native Telegram onboarding, context visibility, and admin controls."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

import logfire
from aiogram import Bot, F, Router, html
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.filters.chat_member_updated import (
    JOIN_TRANSITION,
    ChatMemberUpdatedFilter,
)
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from derp.db import (
    DatabaseManager,
    SharedFactDecisionConflictError,
    acknowledge_context_notice,
    approve_shared_fact,
    claim_member_notice,
    clear_history_scope,
    forget_approved_shared_facts,
    reject_shared_fact,
    remove_disqualified_message,
    set_admin_policy,
    set_ambient_history,
    set_chat_policy_flag,
    set_history_retention,
    tombstone_user_messages,
)
from derp.history.policy import CONTEXT_NOTICE_VERSION, ChatPolicyFlag
from derp.models import Chat as ChatModel
from derp.models import User as UserModel
from derp.observability import report_exception
from derp.operations import (
    OperationLedger,
    WalletActivity,
    WalletActivityKind,
    WalletOwner,
    WalletOwnerKind,
    WalletStatement,
)
from derp.tools.shared_facts import SharedFactAction, SharedFactCallback

router = Router(name="context_settings")
ADMIN_POLICY_PROMPT = (
    "Reply with one short admin policy paragraph, or reply with clear to remove it."
)


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
    FORGET_FACTS_CONFIRM = "forget_facts_confirm"
    FORGET_FACTS = "forget_facts"
    FACT_MEMBER_EDIT = "fact_member_edit"
    SHARED_SPEND = "shared_spend"
    EXPENSIVE_TOOLS = "expensive_tools"
    ADMIN_POLICY = "admin_policy"
    CREDITS = "credits"
    PERSONAL_SPEND = "personal_spend"


class ContextCallback(CallbackData, prefix="ctx"):
    """Authenticated server-side context mutation request."""

    action: ContextAction
    value: int = 0


_POLICY_FLAGS = {
    ContextAction.FACT_MEMBER_EDIT: ChatPolicyFlag.SHARED_FACTS_MEMBER_EDIT,
    ContextAction.SHARED_SPEND: ChatPolicyFlag.SHARED_CREDIT_SPENDING,
    ContextAction.EXPENSIVE_TOOLS: ChatPolicyFlag.EXPENSIVE_TOOLS,
}


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
    if can_manage and chat:
        if not is_private:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=(
                            "Facts: Members"
                            if chat.shared_facts_member_edit
                            else "Facts: Admin review"
                        ),
                        callback_data=ContextCallback(
                            action=ContextAction.FACT_MEMBER_EDIT,
                            value=0 if chat.shared_facts_member_edit else 1,
                        ).pack(),
                    ),
                    InlineKeyboardButton(
                        text=(
                            "Shared spend: On"
                            if chat.shared_credit_spending_enabled
                            else "Shared spend: Off"
                        ),
                        callback_data=ContextCallback(
                            action=ContextAction.SHARED_SPEND,
                            value=0 if chat.shared_credit_spending_enabled else 1,
                        ).pack(),
                    ),
                ]
            )
        rows.append(
            [
                InlineKeyboardButton(
                    text=(
                        "Expensive tools: On"
                        if chat.expensive_tools_enabled
                        else "Expensive tools: Off"
                    ),
                    callback_data=ContextCallback(
                        action=ContextAction.EXPENSIVE_TOOLS,
                        value=0 if chat.expensive_tools_enabled else 1,
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text="Admin policy",
                    callback_data=ContextCallback(
                        action=ContextAction.ADMIN_POLICY
                    ).pack(),
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="Privacy & history",
                callback_data=ContextCallback(action=ContextAction.PRIVACY).pack(),
            ),
            InlineKeyboardButton(
                text="Credits",
                callback_data=ContextCallback(action=ContextAction.CREDITS).pack(),
            ),
        ]
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def build_credit_panel(
    personal: WalletStatement,
    *,
    shared: WalletStatement | None,
    shared_spending_enabled: bool,
    personal_fallback_enabled: bool,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build a truthful balance and per-chat funding preference surface."""
    personal_balance = personal.balance
    allowance_line = f"Monthly allowance: {personal_balance.allowance_available}"
    if personal.allowance_period_end is not None:
        renewal = "renews" if personal.renewal_enabled else "ends"
        allowance_line += (
            f" · {renewal} {personal.allowance_period_end.strftime('%d %b %Y')}"
        )
    elif personal_balance.allowance_available == 0:
        allowance_line += " · no active plan"
    lines = [
        "<b>Credits</b>",
        "",
        "<b>Personal</b>",
        allowance_line,
        f"Purchased: {personal_balance.purchased_available}",
    ]
    if personal_balance.reserved:
        lines.append(f"In progress: {personal_balance.reserved}")
    if personal_balance.debt:
        lines.append(f"Payment debt: {personal_balance.debt} · paid use is paused")
    if personal.recent_activity:
        lines.extend(["", "<b>Recent activity</b>"])
        lines.extend(_wallet_activity_line(item) for item in personal.recent_activity)

    rows: list[list[InlineKeyboardButton]] = []
    if shared is not None:
        shared_balance = shared.balance
        state = "available" if shared_spending_enabled else "paused by admins"
        lines.extend(
            [
                "",
                "<b>This chat</b>",
                f"Shared purchased: {shared_balance.purchased_available} · {state}",
            ]
        )
        if shared_balance.reserved:
            lines.append(f"Shared in progress: {shared_balance.reserved}")
        lines.extend(
            f"Shared {_wallet_activity_line(item).lower()}"
            for item in shared.recent_activity
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=(
                        "Personal fallback: Always"
                        if personal_fallback_enabled
                        else "Personal fallback: Ask me"
                    ),
                    callback_data=ContextCallback(
                        action=ContextAction.PERSONAL_SPEND,
                        value=0 if personal_fallback_enabled else 1,
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
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _wallet_activity_line(activity: WalletActivity) -> str:
    feature = {
        "chat": "Chat",
        "inline_chat": "Inline chat",
        "deep_think": "Deep thinking",
        "image_generate": "Image generation",
        "image_edit": "Image editing",
        "tts": "Voice",
        "video_generate": "Video generation",
    }.get(activity.feature and activity.feature.value, "Paid operation")
    date = activity.occurred_at.strftime("%d %b")
    if activity.kind is WalletActivityKind.CHARGE:
        return f"{feature}: -{activity.credits} · {date}"
    if activity.kind is WalletActivityKind.REFUND:
        return f"{feature} refund: +{activity.credits} · {date}"
    if activity.kind is WalletActivityKind.PAYMENT_CLAWBACK:
        return f"Payment refund: -{activity.credits} · {date}"
    return f"Payment debt: {activity.credits} · {date}"


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
                ),
                InlineKeyboardButton(
                    text="Forget shared facts",
                    callback_data=ContextCallback(
                        action=ContextAction.FORGET_FACTS_CONFIRM
                    ).pack(),
                ),
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
    detail: str = (
        "This removes Derp's stored source, media references, and derived history. "
        "It cannot remove Telegram's copy or text another member copied."
    ),
) -> tuple[str, InlineKeyboardMarkup]:
    """Build a compact confirmation that states Telegram's deletion boundary."""
    text = f"<b>{label}?</b>\n{detail}"
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


async def _credit_panel_for(
    operation_ledger: OperationLedger,
    user_model: UserModel,
    chat_model: ChatModel | None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Load one user's exact wallet view without exposing ledger internals."""
    personal = await operation_ledger.statement(
        WalletOwner(WalletOwnerKind.USER, user_model.id)
    )
    shared = None
    consent_enabled = False
    if chat_model and chat_model.type != "private":
        shared = await operation_ledger.statement(
            WalletOwner(WalletOwnerKind.CHAT, chat_model.id)
        )
        consent_enabled = await operation_ledger.personal_consent_enabled(
            user_model.id, chat_model.id
        )
    return build_credit_panel(
        personal,
        shared=shared,
        shared_spending_enabled=bool(
            chat_model and chat_model.shared_credit_spending_enabled
        ),
        personal_fallback_enabled=consent_enabled,
    )


@router.callback_query(ContextCallback.filter(F.action == ContextAction.CREDITS))
async def show_credit_menu(
    query: CallbackQuery,
    operation_ledger: OperationLedger,
    user_model: UserModel | None,
    chat_model: ChatModel | None,
) -> None:
    """Show personal and shared inventories from the authoritative ledger."""
    if not isinstance(query.message, Message) or not user_model:
        return await query.answer("Credits are unavailable", show_alert=True)
    text, markup = await _credit_panel_for(operation_ledger, user_model, chat_model)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(ContextCallback.filter(F.action == ContextAction.PERSONAL_SPEND))
async def toggle_personal_spend(
    query: CallbackQuery,
    callback_data: ContextCallback,
    operation_ledger: OperationLedger,
    user_model: UserModel | None,
    chat_model: ChatModel | None,
) -> None:
    """Change only the callback actor's durable personal fallback preference."""
    if (
        not isinstance(query.message, Message)
        or not user_model
        or not chat_model
        or chat_model.type == "private"
    ):
        return await query.answer("This preference applies to groups", show_alert=True)
    if user_model.telegram_id != query.from_user.id or callback_data.value not in {
        0,
        1,
    }:
        return await query.answer("Invalid preference", show_alert=True)

    enabled = bool(callback_data.value)
    if enabled:
        await operation_ledger.grant_personal_consent(user_model.id, chat_model.id)
    else:
        await operation_ledger.revoke_personal_consent(user_model.id, chat_model.id)
    text, markup = await _credit_panel_for(operation_ledger, user_model, chat_model)
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer("Personal fallback updated")


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


@router.callback_query(
    ContextCallback.filter(F.action == ContextAction.FORGET_FACTS_CONFIRM)
)
async def confirm_forget_shared_facts(query: CallbackQuery, bot: Bot) -> None:
    """Require live admin authority and confirmation before fact deletion."""
    if not isinstance(query.message, Message):
        return await query.answer()
    if not await actor_can_manage(bot, query.message, query.from_user.id):
        return await query.answer(
            "Only chat admins can forget shared facts",
            show_alert=True,
        )
    scope = "topic" if query.message.message_thread_id is not None else "chat"
    text, markup = build_destructive_confirmation(
        action=ContextAction.FORGET_FACTS,
        label=f"Forget approved facts in this {scope}",
        detail=(
            "This removes approved factual memory in this scope. "
            "Conversation history is unchanged."
        ),
    )
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(ContextCallback.filter(F.action == ContextAction.FORGET_FACTS))
async def forget_current_shared_facts(
    query: CallbackQuery,
    db: DatabaseManager,
    bot: Bot,
    chat_model: ChatModel | None,
) -> None:
    """Delete approved facts without changing conversation history."""
    if not isinstance(query.message, Message) or not chat_model:
        return await query.answer("Shared facts are unavailable", show_alert=True)
    if not await actor_can_manage(bot, query.message, query.from_user.id):
        return await query.answer(
            "Only chat admins can forget shared facts",
            show_alert=True,
        )
    async with db.session() as session:
        removed = await forget_approved_shared_facts(
            session,
            chat_id=chat_model.id,
            thread_id=query.message.message_thread_id,
        )
    text, markup = build_privacy_panel(
        chat_model,
        can_manage=True,
        thread_id=query.message.message_thread_id,
    )
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer(f"Forgot {removed} approved facts")


@router.callback_query(SharedFactCallback.filter())
async def review_shared_fact_proposal(
    query: CallbackQuery,
    callback_data: SharedFactCallback,
    db: DatabaseManager,
    bot: Bot,
    chat_model: ChatModel | None,
    user_model: UserModel | None,
) -> None:
    """Approve or reject one exact topic-scoped proposal after live authorization."""
    if not isinstance(query.message, Message) or not chat_model or not user_model:
        return await query.answer("This proposal is unavailable", show_alert=True)
    is_admin = await actor_can_manage(bot, query.message, query.from_user.id)
    if not is_admin and not chat_model.shared_facts_member_edit:
        return await query.answer("An admin must review this fact", show_alert=True)
    try:
        fact_id = UUID(callback_data.fact_id)
    except ValueError:
        return await query.answer("Invalid proposal", show_alert=True)
    try:
        async with db.session() as session:
            decide = (
                approve_shared_fact
                if callback_data.action is SharedFactAction.APPROVE
                else reject_shared_fact
            )
            fact = await decide(
                session,
                fact_id=fact_id,
                chat_id=chat_model.id,
                thread_id=query.message.message_thread_id,
                admin_actor_id=user_model.id,
            )
    except (LookupError, SharedFactDecisionConflictError) as exc:
        return await query.answer(str(exc), show_alert=True)

    state = (
        "Approved" if callback_data.action is SharedFactAction.APPROVE else "Rejected"
    )
    await query.message.edit_text(
        f"<b>{state} shared fact</b>\n"
        f"<blockquote>{html.quote(fact.fact_text)}</blockquote>"
    )
    await query.answer(state)


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


@router.callback_query(ContextCallback.filter(F.action.in_(set(_POLICY_FLAGS))))
async def change_policy_flag(
    query: CallbackQuery,
    callback_data: ContextCallback,
    db: DatabaseManager,
    bot: Bot,
    chat_model: ChatModel | None,
) -> None:
    """Apply one typed policy flag after live admin authorization."""
    if not isinstance(query.message, Message) or not chat_model:
        return await query.answer("Settings are unavailable", show_alert=True)
    if not await actor_can_manage(bot, query.message, query.from_user.id):
        return await query.answer("Only chat admins can change this", show_alert=True)
    if callback_data.value not in {0, 1}:
        return await query.answer("Invalid setting", show_alert=True)
    flag = _POLICY_FLAGS[callback_data.action]
    if chat_model.type == "private" and flag is not ChatPolicyFlag.EXPENSIVE_TOOLS:
        return await query.answer("This setting applies to groups", show_alert=True)
    enabled = bool(callback_data.value)
    async with db.session() as session:
        await set_chat_policy_flag(
            session,
            chat_telegram_id=query.message.chat.id,
            flag=flag,
            enabled=enabled,
        )
    setattr(chat_model, flag.value, enabled)
    available = query.message.chat.type == "private" or (
        await ambient_delivery_available(bot, query.message.chat.id)
    )
    text, markup = build_context_panel(
        chat_model,
        ambient_available=available,
        can_manage=True,
    )
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer("Setting updated")


@router.callback_query(ContextCallback.filter(F.action == ContextAction.ADMIN_POLICY))
async def request_admin_policy(
    query: CallbackQuery,
    bot: Bot,
) -> None:
    """Request one bounded trusted policy paragraph through a native reply."""
    if not isinstance(query.message, Message):
        return await query.answer()
    if not await actor_can_manage(bot, query.message, query.from_user.id):
        return await query.answer("Only chat admins can set policy", show_alert=True)
    await query.message.answer(
        ADMIN_POLICY_PROMPT,
        reply_markup=ForceReply(
            selective=True,
            input_field_placeholder="One policy paragraph",
        ),
    )
    await query.answer()


@router.message(F.reply_to_message.text == ADMIN_POLICY_PROMPT)
async def save_admin_policy(
    message: Message,
    db: DatabaseManager,
    bot: Bot,
    chat_model: ChatModel | None,
) -> None:
    """Store admin-authored instructions separately from conversation data."""
    if not chat_model or not message.from_user:
        return
    if not await actor_can_manage(bot, message, message.from_user.id):
        await message.reply("Only chat admins can set policy.")
        return
    submitted = (message.text or "").strip()
    policy = None if submitted.casefold() == "clear" else submitted
    try:
        async with db.session() as session:
            stored = await set_admin_policy(
                session,
                chat_telegram_id=message.chat.id,
                policy=policy,
            )
            await remove_disqualified_message(
                session,
                chat_telegram_id=message.chat.id,
                telegram_message_id=message.message_id,
            )
    except ValueError as exc:
        await message.reply(str(exc))
        return
    chat_model.admin_policy = stored
    await message.reply("Admin policy updated." if stored else "Admin policy cleared.")


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
    "build_credit_panel",
    "build_context_panel",
    "build_destructive_confirmation",
    "build_privacy_panel",
    "ensure_group_context_notice",
    "router",
]
