"""Native Telegram onboarding, context visibility, and admin controls."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated
from uuid import UUID

import logfire
from aiogram import Bot, F, Router, html
from aiogram.filters import BaseFilter, Command
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
from aiogram.utils.i18n import gettext as _
from pydantic import Field

from derp.command_menu import creation_command_specs
from derp.common.legal_documents import LegalDocumentKind
from derp.common.localization import format_local_date, format_local_month_day
from derp.config import settings
from derp.db import (
    DatabaseManager,
    SharedFactDecisionConflictError,
    accept_non_zdr_free_inference,
    acknowledge_context_notice,
    approve_shared_fact,
    claim_member_notice,
    clear_history_scope,
    enable_chat_non_zdr_free_inference,
    forget_approved_shared_facts,
    get_chat_free_model_policy,
    get_inference_privacy_preference,
    reject_shared_fact,
    remove_disqualified_message,
    revoke_chat_non_zdr_free_inference,
    revoke_non_zdr_free_inference,
    set_admin_policy,
    set_ambient_history,
    set_chat_policy_flag,
    set_history_retention,
    tombstone_user_messages,
)
from derp.db.inference_privacy import (
    ChatFreeModelRevisionConflictError,
    InferencePrivacyRevisionConflictError,
)
from derp.handlers.legal_support import (
    SUPPORT_MENU_CALLBACK,
    LegalDocumentCallback,
)
from derp.history.policy import CONTEXT_NOTICE_VERSION, ChatPolicyFlag
from derp.inference import (
    FREE_INFERENCE_PRIVACY_VERSION,
    FREE_INFERENCE_TOS_VERSION,
    ChatFreeModelPolicy,
    InferenceContext,
    InferencePrivacyPreference,
    NonZdrFreeInferenceReason,
    decide_non_zdr_free_inference,
    project_chat_free_model_policy,
    project_inference_privacy,
)
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


def admin_policy_prompt() -> str:
    """Return the ForceReply prompt in the current request locale."""
    return _(
        "Reply with one short instruction for this chat, or send {clear} to remove it."
    ).format(clear='"clear"')


class AdminPolicyReplyFilter(BaseFilter):
    """Match the localized ForceReply prompt in the current request locale."""

    async def __call__(self, message: Message) -> bool:
        reply = message.reply_to_message
        return bool(reply and reply.text == admin_policy_prompt())


class ContextAction(StrEnum):
    """Compact typed callback actions for the context panel."""

    MENU = "menu"
    CREATION = "creation"
    PRIVACY = "privacy"
    INFERENCE_PRIVACY = "model_privacy"
    TOGGLE = "toggle"
    TOGGLE_CONFIRM = "toggle_confirm"
    CLEAN_AMBIENT = "clean_ambient"
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


class InferencePrivacyAction(StrEnum):
    """Version-bound actions for the free-model privacy preference."""

    REVIEW = "r"
    ACCEPT = "a"
    REVOKE = "x"


class InferencePrivacyCallback(CallbackData, prefix="ifp"):
    """Bind a legal preference action to the documents the user reviewed."""

    action: InferencePrivacyAction
    tos_version: str
    privacy_version: str
    preference_revision: Annotated[int, Field(ge=1, le=2_147_483_647)]


class ChatFreeModelAction(StrEnum):
    """Version-bound administrator actions for one chat's free fallback."""

    REVIEW = "r"
    ACCEPT = "a"
    REVOKE = "x"


class ChatFreeModelCallback(CallbackData, prefix="cfm"):
    """Bind a chat-wide policy action to reviewed legal documents."""

    action: ChatFreeModelAction
    tos_version: str
    privacy_version: str
    policy_revision: Annotated[int, Field(ge=1, le=2_147_483_647)]


_POLICY_FLAGS = {
    ContextAction.FACT_MEMBER_EDIT: ChatPolicyFlag.SHARED_FACTS_MEMBER_EDIT,
    ContextAction.SHARED_SPEND: ChatPolicyFlag.SHARED_CREDIT_SPENDING,
    ContextAction.EXPENSIVE_TOOLS: ChatPolicyFlag.EXPENSIVE_TOOLS,
}


def _policy_flag_notice(flag: ChatPolicyFlag, *, enabled: bool) -> str:
    if flag is ChatPolicyFlag.SHARED_FACTS_MEMBER_EDIT:
        return (
            _("Members can now edit shared facts")
            if enabled
            else _("Shared facts now need admin review")
        )
    if flag is ChatPolicyFlag.SHARED_CREDIT_SPENDING:
        return (
            _("Chat credits are available") if enabled else _("Chat credits are paused")
        )
    return (
        _("Paid features are enabled") if enabled else _("Paid features are disabled")
    )


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
    is_group = bool(chat and chat.type in {"group", "supergroup"})
    enabled = bool(chat and chat.ambient_history_enabled and ambient_available)
    free_policy = (
        project_chat_free_model_policy(chat)
        if chat is not None and is_group
        else ChatFreeModelPolicy()
    )
    free_decision = decide_non_zdr_free_inference(
        InferencePrivacyPreference(),
        context=InferenceContext.SUPERGROUP,
        current_tos_version=FREE_INFERENCE_TOS_VERSION,
        current_privacy_version=FREE_INFERENCE_PRIVACY_VERSION,
        chat_policy=free_policy,
    )
    if is_private:
        state = _("On")
        context_line = _(
            "History: On · {days} day",
            "History: On · {days} days",
            retention_days,
        ).format(days=retention_days)
    elif ambient_available:
        state = _("On") if enabled else _("Off")
        context_line = _(
            "Context: {state} · {days} day",
            "Context: {state} · {days} days",
            retention_days,
        ).format(
            state=state,
            days=retention_days,
        )
    else:
        state = _("Mentions only")
        context_line = _("Context: Mentions only")

    free_line = ""
    if is_group:
        free_state = (
            _("On")
            if free_decision.allowed
            else _("Needs review")
            if free_policy.enabled
            else _("Off")
        )
        free_line = _("\nFree models: {state}").format(state=free_state)

    text = _(
        "<b>Derp</b>\n"
        "Message me privately, or mention or reply to me in a group.\n\n"
        "{context_line}{free_line}\n"
        "Recent messages help me answer follow-ups. Anyone can check this setting "
        "and delete their own saved messages from my memory."
    ).format(context_line=context_line, free_line=free_line)
    rows = [
        [
            InlineKeyboardButton(
                text=_("Ask Derp"),
                switch_inline_query_current_chat="",
            ),
            InlineKeyboardButton(
                text=(
                    _("History: {days}d").format(days=retention_days)
                    if is_private
                    else _("Turn context off")
                    if can_manage and enabled
                    else _("Turn context on")
                    if can_manage
                    else _("Context: {state}").format(state=state)
                ),
                callback_data=ContextCallback(
                    action=(
                        ContextAction.PRIVACY
                        if is_private
                        else ContextAction.TOGGLE_CONFIRM
                        if can_manage and enabled
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
                    text=_("{days}d{selected}").format(
                        days=days,
                        selected=" ✓" if retention_days == days else "",
                    ),
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
            if is_group:
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=(
                                _("Disable free models")
                                if free_decision.allowed
                                else _("Review free-model privacy")
                            ),
                            callback_data=ChatFreeModelCallback(
                                action=(
                                    ChatFreeModelAction.REVOKE
                                    if free_decision.allowed
                                    else ChatFreeModelAction.REVIEW
                                ),
                                tos_version=FREE_INFERENCE_TOS_VERSION,
                                privacy_version=FREE_INFERENCE_PRIVACY_VERSION,
                                policy_revision=free_policy.revision,
                            ).pack(),
                        )
                    ]
                )
            rows.append(
                [
                    InlineKeyboardButton(
                        text=(
                            _("Require admin fact review")
                            if chat.shared_facts_member_edit
                            else _("Let members edit facts")
                        ),
                        callback_data=ContextCallback(
                            action=ContextAction.FACT_MEMBER_EDIT,
                            value=0 if chat.shared_facts_member_edit else 1,
                        ).pack(),
                    ),
                    InlineKeyboardButton(
                        text=(
                            _("Pause chat credits")
                            if chat.shared_credit_spending_enabled
                            else _("Enable chat credits")
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
                        _("Disable paid features")
                        if chat.expensive_tools_enabled
                        else _("Enable paid features")
                    ),
                    callback_data=ContextCallback(
                        action=ContextAction.EXPENSIVE_TOOLS,
                        value=0 if chat.expensive_tools_enabled else 1,
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text=_("Set chat instructions"),
                    callback_data=ContextCallback(
                        action=ContextAction.ADMIN_POLICY
                    ).pack(),
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=_("Create"),
                callback_data=ContextCallback(action=ContextAction.CREATION).pack(),
            ),
            InlineKeyboardButton(
                text=_("Credits"),
                callback_data=ContextCallback(action=ContextAction.CREDITS).pack(),
            ),
        ]
    )
    personal_rows = [
        InlineKeyboardButton(
            text=_("Privacy & history"),
            callback_data=ContextCallback(action=ContextAction.PRIVACY).pack(),
        )
    ]
    if not is_private:
        personal_rows.append(
            InlineKeyboardButton(
                text=_("Change how my credits are used"),
                callback_data=ContextCallback(
                    action=ContextAction.PERSONAL_SPEND,
                    value=-1,
                ).pack(),
            )
        )
    rows.append(personal_rows)
    if is_private:
        rows.append(
            [
                InlineKeyboardButton(
                    text=_("Model privacy"),
                    callback_data=ContextCallback(
                        action=ContextAction.INFERENCE_PRIVACY
                    ).pack(),
                )
            ]
        )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def build_creation_panel() -> tuple[str, InlineKeyboardMarkup]:
    """Render the small set of creation paths that are safe to advertise."""
    commands = creation_command_specs()
    lines = [
        f"<b>{html.quote(_('Create'))}</b>",
        "",
        *(
            f"{html.code(f'/{command.command}')} - {html.quote(command.description)}"
            for command in commands
        ),
    ]
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_("Back"),
                    callback_data=ContextCallback(action=ContextAction.MENU).pack(),
                )
            ]
        ]
    )
    return "\n".join(lines), markup


def build_credit_panel(
    personal: WalletStatement,
    *,
    shared: WalletStatement | None,
    shared_spending_enabled: bool,
    personal_fallback_enabled: bool,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build a truthful balance and per-chat funding preference surface."""
    personal_balance = personal.balance
    allowance_line = _(
        "Monthly plan: {credits} credit",
        "Monthly plan: {credits} credits",
        personal_balance.allowance_available,
    ).format(credits=personal_balance.allowance_available)
    if personal.allowance_period_end is not None:
        renewal = _("renews") if personal.renewal_enabled else _("ends")
        allowance_line = _("{allowance} · {renewal} {date}").format(
            allowance=allowance_line,
            renewal=renewal,
            date=format_local_date(personal.allowance_period_end),
        )
    elif personal_balance.allowance_available == 0:
        allowance_line = _("{allowance} · no active plan").format(
            allowance=allowance_line
        )
    lines = [
        _("<b>Credits</b>"),
        "",
        _("<b>Your credits</b>"),
        allowance_line,
        _(
            "Purchased: {credits} credit",
            "Purchased: {credits} credits",
            personal_balance.purchased_available,
        ).format(credits=personal_balance.purchased_available),
    ]
    if personal_balance.reserved:
        lines.append(
            _(
                "Reserved: {credits} credit",
                "Reserved: {credits} credits",
                personal_balance.reserved,
            ).format(credits=personal_balance.reserved)
        )
    if personal_balance.debt:
        lines.append(
            _(
                "Payment debt: {credits} credit · paid features are paused",
                "Payment debt: {credits} credits · paid features are paused",
                personal_balance.debt,
            ).format(credits=personal_balance.debt)
        )
    if personal.recent_activity:
        lines.extend(["", _("<b>Recent activity</b>")])
        lines.extend(_wallet_activity_line(item) for item in personal.recent_activity)

    rows: list[list[InlineKeyboardButton]] = []
    if shared is not None:
        shared_balance = shared.balance
        state = _("available") if shared_spending_enabled else _("paused by admins")
        lines.extend(
            [
                "",
                _("<b>This chat's credits</b>"),
                _(
                    "Purchased: {credits} credit · {state}",
                    "Purchased: {credits} credits · {state}",
                    shared_balance.purchased_available,
                ).format(
                    credits=shared_balance.purchased_available,
                    state=state,
                ),
            ]
        )
        if shared_balance.reserved:
            lines.append(
                _(
                    "Reserved: {credits} credit",
                    "Reserved: {credits} credits",
                    shared_balance.reserved,
                ).format(credits=shared_balance.reserved)
            )
        if shared.recent_activity:
            lines.extend(["", _("<b>Recent chat activity</b>")])
            lines.extend(_wallet_activity_line(item) for item in shared.recent_activity)
        rows.append(
            [
                InlineKeyboardButton(
                    text=(
                        _("Ask before using my credits")
                        if personal_fallback_enabled
                        else _("Use my credits without asking")
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
                text=_("Back"),
                callback_data=ContextCallback(action=ContextAction.MENU).pack(),
            )
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _wallet_activity_line(activity: WalletActivity) -> str:
    feature = {
        "chat": _("Chat"),
        "inline_chat": _("Inline chat"),
        "deep_think": _("Deep thinking"),
        "image_generate": _("Image generation"),
        "image_edit": _("Image editing"),
        "tts": _("Voice"),
        "video_generate": _("Video generation"),
    }.get(activity.feature and activity.feature.value, _("Paid feature"))
    date = format_local_month_day(activity.occurred_at)
    if activity.kind is WalletActivityKind.CHARGE:
        return _(
            "{feature}: -{credits} credit · {date}",
            "{feature}: -{credits} credits · {date}",
            activity.credits,
        ).format(
            feature=feature,
            credits=activity.credits,
            date=date,
        )
    if activity.kind is WalletActivityKind.REFUND:
        return _(
            "{feature} refund: +{credits} credit · {date}",
            "{feature} refund: +{credits} credits · {date}",
            activity.credits,
        ).format(
            feature=feature,
            credits=activity.credits,
            date=date,
        )
    if activity.kind is WalletActivityKind.PAYMENT_CLAWBACK:
        return _(
            "Payment refund: -{credits} credit · {date}",
            "Payment refund: -{credits} credits · {date}",
            activity.credits,
        ).format(
            credits=activity.credits,
            date=date,
        )
    return _(
        "Payment debt: {credits} credit · {date}",
        "Payment debt: {credits} credits · {date}",
        activity.credits,
    ).format(
        credits=activity.credits,
        date=date,
    )


def build_privacy_panel(
    chat: ChatModel | None,
    *,
    can_manage: bool,
    thread_id: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build discoverable personal deletion and scoped admin cleanup controls."""
    retention_days = chat.retention_days if chat else 30
    scope_label = (
        _("topic")
        if thread_id is not None
        else _("whole chat")
        if chat and chat.is_forum
        else _("chat")
    )
    rows = [
        [
            InlineKeyboardButton(
                text=_("Delete from my memory"),
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
                    text=_("Clear this {scope}").format(scope=scope_label),
                    callback_data=ContextCallback(
                        action=ContextAction.CLEAR_CONFIRM
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text=_("Forget shared facts"),
                    callback_data=ContextCallback(
                        action=ContextAction.FORGET_FACTS_CONFIRM
                    ).pack(),
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=_("Privacy policy"),
                callback_data=LegalDocumentCallback(
                    document=LegalDocumentKind.PRIVACY,
                    separate=True,
                ).pack(),
            ),
            InlineKeyboardButton(
                text=_("Terms of use"),
                callback_data=LegalDocumentCallback(
                    document=LegalDocumentKind.TERMS,
                    separate=True,
                ).pack(),
            ),
        ]
    )
    support_button = (
        InlineKeyboardButton(
            text=_("Contact support"),
            callback_data=SUPPORT_MENU_CALLBACK,
        )
        if chat is None or chat.type == "private"
        else InlineKeyboardButton(
            text=_("Contact support"),
            url=(
                f"https://t.me/{settings.bot_username.removeprefix('@')}?start=support"
            ),
        )
    )
    rows.append([support_button])
    rows.append(
        [
            InlineKeyboardButton(
                text=_("Back"),
                callback_data=ContextCallback(action=ContextAction.MENU).pack(),
            )
        ]
    )
    text = _(
        "<b>Privacy and history</b>\n"
        "Saved messages are deleted after {days} day.\n\n"
        "You can delete your own saved messages from my memory at any time. Chat admins can clear "
        "this chat or topic. Approved shared facts are kept separately.",
        "<b>Privacy and history</b>\n"
        "Saved messages are deleted after {days} days.\n\n"
        "You can delete your own saved messages from my memory at any time. Chat admins can clear "
        "this chat or topic. Approved shared facts are kept separately.",
        retention_days,
    ).format(days=retention_days)
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def build_inference_privacy_panel(
    preference: InferencePrivacyPreference,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render the effective inference mode and its single next action."""
    decision = decide_non_zdr_free_inference(
        preference,
        context=InferenceContext.PRIVATE,
        current_tos_version=FREE_INFERENCE_TOS_VERSION,
        current_privacy_version=FREE_INFERENCE_PRIVACY_VERSION,
    )
    if decision.allowed:
        mode = _("Mode: Free models allowed")
        detail = _(
            "Free-model providers may store prompts and replies. This permission "
            "applies only in private chat and inline mode. Group admins choose "
            "separately for each chat."
        )
        label = _("Use private models only")
        action = InferencePrivacyAction.REVOKE
    else:
        mode = _("Mode: Private")
        if decision.reason is NonZdrFreeInferenceReason.LEGAL_REACCEPTANCE_REQUIRED:
            detail = _(
                "The free-model terms changed. Review them to enable free models "
                "again. Group admins choose separately for each chat."
            )
        else:
            detail = _(
                "Derp uses zero-data-retention models. Free models are optional and "
                "may let providers store prompts and replies. They can run in "
                "private chat and inline mode. Group admins can enable them "
                "separately for a chat."
            )
        label = _("Review free-model terms")
        action = InferencePrivacyAction.REVIEW

    text = _("<b>Model privacy</b>\n{mode}\n\n{detail}").format(
        mode=mode,
        detail=detail,
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=InferencePrivacyCallback(
                        action=action,
                        tos_version=FREE_INFERENCE_TOS_VERSION,
                        privacy_version=FREE_INFERENCE_PRIVACY_VERSION,
                        preference_revision=preference.revision,
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=_("Back"),
                    callback_data=ContextCallback(action=ContextAction.MENU).pack(),
                )
            ],
        ]
    )
    return text, markup


def build_inference_privacy_review(
    preference_revision: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """Show the legal review immediately before explicit acceptance."""
    text = _(
        "<b>Allow free models?</b>\n\n"
        "Free-model providers may store prompts and replies under their own "
        "policies. By continuing, you allow Derp to send prompts and replies to "
        "OpenRouter and selected free-model providers under the linked Terms and "
        "Privacy Policy.\n\n"
        "This applies to your private chat and inline mode. Group admins make a "
        "separate choice for each chat."
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_("Terms"),
                    callback_data=LegalDocumentCallback(
                        document=LegalDocumentKind.TERMS,
                        separate=True,
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text=_("Privacy"),
                    callback_data=LegalDocumentCallback(
                        document=LegalDocumentKind.PRIVACY,
                        separate=True,
                    ).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_("I agree, allow free models"),
                    callback_data=InferencePrivacyCallback(
                        action=InferencePrivacyAction.ACCEPT,
                        tos_version=FREE_INFERENCE_TOS_VERSION,
                        privacy_version=FREE_INFERENCE_PRIVACY_VERSION,
                        preference_revision=preference_revision,
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=_("Back"),
                    callback_data=ContextCallback(
                        action=ContextAction.INFERENCE_PRIVACY
                    ).pack(),
                )
            ],
        ]
    )
    return text, markup


def build_chat_free_model_review(
    policy_revision: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """Explain the chat-wide privacy choice before an admin accepts it."""
    text = _(
        "<b>Allow free models in this chat?</b>\n\n"
        "When chat credits are unavailable, Derp may send prompts and replies to "
        "OpenRouter and selected free-model providers. Those providers may store "
        "them under their own policies.\n\n"
        "Everyone in this chat will see a notice. Admins should also tell members "
        "who join later."
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_("Terms"),
                    callback_data=LegalDocumentCallback(
                        document=LegalDocumentKind.TERMS,
                        separate=True,
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text=_("Privacy"),
                    callback_data=LegalDocumentCallback(
                        document=LegalDocumentKind.PRIVACY,
                        separate=True,
                    ).pack(),
                ),
            ],
            [
                InlineKeyboardButton(
                    text=_("Allow free models in this chat"),
                    callback_data=ChatFreeModelCallback(
                        action=ChatFreeModelAction.ACCEPT,
                        tos_version=FREE_INFERENCE_TOS_VERSION,
                        privacy_version=FREE_INFERENCE_PRIVACY_VERSION,
                        policy_revision=policy_revision,
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=_("Back"),
                    callback_data=ContextCallback(action=ContextAction.MENU).pack(),
                )
            ],
        ]
    )
    return text, markup


def build_free_model_recovery(
    user: UserModel,
    chat: ChatModel,
    *,
    context: InferenceContext,
    can_manage: bool,
) -> tuple[str, InlineKeyboardMarkup]:
    """Return a direct recovery path when neither paid nor free chat can run."""
    chat_policy = (
        project_chat_free_model_policy(chat)
        if context in {InferenceContext.GROUP, InferenceContext.SUPERGROUP}
        else None
    )
    decision = decide_non_zdr_free_inference(
        project_inference_privacy(user),
        context=context,
        current_tos_version=FREE_INFERENCE_TOS_VERSION,
        current_privacy_version=FREE_INFERENCE_PRIVACY_VERSION,
        chat_policy=chat_policy,
    )

    if decision.allowed:
        text = _(
            "No free model can handle this request. Try without the attachment, "
            "or add credits with /buy."
        )
        button = InlineKeyboardButton(
            text=_("Open settings"),
            callback_data=ContextCallback(action=ContextAction.MENU).pack(),
        )
    elif context in {InferenceContext.PRIVATE, InferenceContext.INLINE}:
        text = _("No credits left. Enable free models, or add credits with /buy.")
        preference = project_inference_privacy(user)
        button = InlineKeyboardButton(
            text=_("Enable free models"),
            callback_data=InferencePrivacyCallback(
                action=InferencePrivacyAction.REVIEW,
                tos_version=FREE_INFERENCE_TOS_VERSION,
                privacy_version=FREE_INFERENCE_PRIVACY_VERSION,
                preference_revision=preference.revision,
            ).pack(),
        )
    elif context not in {InferenceContext.GROUP, InferenceContext.SUPERGROUP}:
        text = _("Free models aren't available here. Add credits with /buy.")
        button = InlineKeyboardButton(
            text=_("Open settings"),
            callback_data=ContextCallback(action=ContextAction.MENU).pack(),
        )
    elif can_manage:
        text = _(
            "This chat is out of credits. Enable free models, or add chat credits "
            "with /buy."
        )
        policy = chat_policy or ChatFreeModelPolicy()
        button = InlineKeyboardButton(
            text=_("Enable free models"),
            callback_data=ChatFreeModelCallback(
                action=ChatFreeModelAction.REVIEW,
                tos_version=FREE_INFERENCE_TOS_VERSION,
                privacy_version=FREE_INFERENCE_PRIVACY_VERSION,
                policy_revision=policy.revision,
            ).pack(),
        )
    else:
        text = _(
            "This chat is out of credits. An admin can enable free models in "
            "/settings. Anyone can add chat credits with /buy."
        )
        button = InlineKeyboardButton(
            text=_("Open chat settings"),
            callback_data=ContextCallback(action=ContextAction.MENU).pack(),
        )

    return text, InlineKeyboardMarkup(inline_keyboard=[[button]])


def build_destructive_confirmation(
    *,
    action: ContextAction,
    label: str,
    detail: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build a compact confirmation that states Telegram's deletion boundary."""
    detail = detail or _(
        "This deletes Derp's saved messages and media references. Telegram messages "
        "and copies made by other people remain."
    )
    text = _("<b>{label}?</b>\n{detail}").format(label=label, detail=detail)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_("Delete"),
                    callback_data=ContextCallback(action=action).pack(),
                ),
                InlineKeyboardButton(
                    text=_("Cancel"),
                    callback_data=ContextCallback(action=ContextAction.PRIVACY).pack(),
                ),
            ]
        ]
    )
    return text, markup


def build_ambient_cleanup_confirmation() -> tuple[str, InlineKeyboardMarkup]:
    """Name the full-chat scope before disabling and purging ambient history."""
    text = _(
        "<b>Clean up my memory?</b>\n"
        "This turns context off and deletes ambient messages saved from this "
        "whole chat, including every topic, from my memory. Mentions and replies "
        "may still be saved. Telegram messages stay."
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_("Clean up memory"),
                    callback_data=ContextCallback(
                        action=ContextAction.CLEAN_AMBIENT,
                        value=0,
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text=_("Cancel"),
                    callback_data=ContextCallback(action=ContextAction.MENU).pack(),
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


@router.message(Command("privacy"))
async def show_privacy_controls(
    message: Message,
    bot: Bot,
    chat_model: ChatModel | None,
) -> None:
    """Open deletion controls and public policies without a settings detour."""
    can_manage = bool(
        message.from_user and await actor_can_manage(bot, message, message.from_user.id)
    )
    text, markup = build_privacy_panel(
        chat_model,
        can_manage=can_manage,
        thread_id=message.message_thread_id,
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


@router.callback_query(ContextCallback.filter(F.action == ContextAction.CREATION))
async def show_creation_menu(query: CallbackQuery) -> None:
    """Open the command-backed creation surface without exposing suspended work."""
    if not isinstance(query.message, Message):
        return await query.answer()
    text, markup = build_creation_panel()
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


async def _credit_panel_for(
    operation_ledger: OperationLedger,
    user_model: UserModel,
    chat_model: ChatModel | None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Load one user's exact wallet view without exposing ledger internals."""
    personal, shared, consent_enabled = await _credit_statements_for(
        operation_ledger,
        user_model,
        chat_model,
    )
    return build_credit_panel(
        personal,
        shared=shared,
        shared_spending_enabled=bool(
            chat_model and chat_model.shared_credit_spending_enabled
        ),
        personal_fallback_enabled=consent_enabled,
    )


async def _credit_statements_for(
    operation_ledger: OperationLedger,
    user_model: UserModel,
    chat_model: ChatModel | None,
) -> tuple[WalletStatement, WalletStatement | None, bool]:
    """Load actor-scoped wallet facts for private rendering."""
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
    return personal, shared, consent_enabled


def _private_credit_alert(
    personal: WalletStatement,
    shared: WalletStatement | None,
    *,
    shared_spending_enabled: bool,
    personal_fallback_enabled: bool,
) -> str:
    """Fit sensitive wallet facts into an actor-only Telegram alert."""
    balance = personal.balance
    lines = [
        _("Your credits"),
        _(
            "Monthly plan: {credits} credit",
            "Monthly plan: {credits} credits",
            balance.allowance_available,
        ).format(credits=balance.allowance_available),
        _(
            "Purchased: {credits} credit",
            "Purchased: {credits} credits",
            balance.purchased_available,
        ).format(credits=balance.purchased_available),
    ]
    if balance.debt:
        lines.append(
            _(
                "Payment debt: {credits} credit",
                "Payment debt: {credits} credits",
                balance.debt,
            ).format(credits=balance.debt)
        )
    if shared is not None:
        shared_state = _("available") if shared_spending_enabled else _("paused")
        lines.append(
            _(
                "This chat: {credits} purchased credit ({state})",
                "This chat: {credits} purchased credits ({state})",
                shared.balance.purchased_available,
            ).format(
                credits=shared.balance.purchased_available,
                state=shared_state,
            )
        )
        personal_use = _("always") if personal_fallback_enabled else _("ask first")
        lines.append(_("Use my credits here: {state}").format(state=personal_use))
    return "\n".join(lines)


@router.callback_query(ContextCallback.filter(F.action == ContextAction.CREDITS))
async def show_credit_menu(
    query: CallbackQuery,
    operation_ledger: OperationLedger,
    user_model: UserModel | None,
    chat_model: ChatModel | None,
) -> None:
    """Show wallet facts without exposing a member's balance to the group."""
    if (
        not isinstance(query.message, Message)
        or not user_model
        or user_model.telegram_id != query.from_user.id
    ):
        return await query.answer(_("Credits are unavailable"), show_alert=True)
    if query.message.chat.type != "private":
        personal, shared, consent_enabled = await _credit_statements_for(
            operation_ledger,
            user_model,
            chat_model,
        )
        await query.answer(
            _private_credit_alert(
                personal,
                shared,
                shared_spending_enabled=bool(
                    chat_model and chat_model.shared_credit_spending_enabled
                ),
                personal_fallback_enabled=consent_enabled,
            ),
            show_alert=True,
        )
        return
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
        return await query.answer(
            _("This setting only applies in groups"), show_alert=True
        )
    if user_model.telegram_id != query.from_user.id or callback_data.value not in {
        -1,
        0,
        1,
    }:
        return await query.answer(_("This setting is no longer valid"), show_alert=True)

    enabled = (
        not await operation_ledger.personal_consent_enabled(
            user_model.id,
            chat_model.id,
        )
        if callback_data.value == -1
        else bool(callback_data.value)
    )
    if enabled:
        await operation_ledger.grant_personal_consent(user_model.id, chat_model.id)
    else:
        await operation_ledger.revoke_personal_consent(user_model.id, chat_model.id)
    await query.answer(
        (
            _("Your credits can be used here without asking")
            if enabled
            else _("I'll ask before using your credits here")
        ),
        show_alert=True,
    )


async def _private_inference_privacy_message(
    query: CallbackQuery,
    user_model: UserModel | None,
) -> Message | None:
    message = query.message
    if (
        not isinstance(message, Message)
        or message.chat.type != "private"
        or message.chat.id != query.from_user.id
        or not user_model
        or user_model.telegram_id != query.from_user.id
    ):
        await query.answer(
            _("Open model privacy in your private chat."), show_alert=True
        )
        return None
    return message


def _current_free_inference_legal_versions(
    callback_data: InferencePrivacyCallback,
) -> bool:
    return (
        callback_data.tos_version == FREE_INFERENCE_TOS_VERSION
        and callback_data.privacy_version == FREE_INFERENCE_PRIVACY_VERSION
    )


async def _show_current_inference_privacy_review(
    query: CallbackQuery,
    message: Message,
    *,
    preference_revision: int,
    terms_changed: bool = False,
) -> None:
    text, markup = build_inference_privacy_review(preference_revision)
    await message.edit_text(text, reply_markup=markup)
    if terms_changed:
        await query.answer(
            _("The terms changed. Review the latest versions."), show_alert=True
        )
    else:
        await query.answer()


async def _show_stale_inference_privacy_panel(
    query: CallbackQuery,
    message: Message,
    preference: InferencePrivacyPreference,
) -> None:
    text, markup = build_inference_privacy_panel(preference)
    await message.edit_text(text, reply_markup=markup)
    await query.answer(
        _("This model privacy button expired. Open the setting again."),
        show_alert=True,
    )


@router.callback_query(
    ContextCallback.filter(F.action == ContextAction.INFERENCE_PRIVACY)
)
async def show_inference_privacy_menu(
    query: CallbackQuery,
    db: DatabaseManager,
    user_model: UserModel | None,
) -> None:
    """Show one actor's effective model privacy mode in private chat only."""
    if (message := await _private_inference_privacy_message(query, user_model)) is None:
        return
    async with db.read_session() as session:
        preference = await get_inference_privacy_preference(session, user_model.id)
    text, markup = build_inference_privacy_panel(preference)
    await message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(
    InferencePrivacyCallback.filter(F.action == InferencePrivacyAction.REVIEW)
)
async def review_inference_privacy_terms(
    query: CallbackQuery,
    callback_data: InferencePrivacyCallback,
    db: DatabaseManager,
    user_model: UserModel | None,
) -> None:
    """Place the current legal documents immediately before acceptance."""
    if (message := await _private_inference_privacy_message(query, user_model)) is None:
        return
    async with db.read_session() as session:
        preference = await get_inference_privacy_preference(session, user_model.id)
    if callback_data.preference_revision != preference.revision:
        return await _show_stale_inference_privacy_panel(
            query,
            message,
            preference,
        )
    await _show_current_inference_privacy_review(
        query,
        message,
        preference_revision=preference.revision,
        terms_changed=not _current_free_inference_legal_versions(callback_data),
    )


@router.callback_query(
    InferencePrivacyCallback.filter(F.action == InferencePrivacyAction.ACCEPT)
)
async def accept_inference_privacy_terms(
    query: CallbackQuery,
    callback_data: InferencePrivacyCallback,
    db: DatabaseManager,
    user_model: UserModel | None,
) -> None:
    """Persist explicit acceptance of exactly the reviewed legal versions."""
    if (message := await _private_inference_privacy_message(query, user_model)) is None:
        return
    if not _current_free_inference_legal_versions(callback_data):
        async with db.read_session() as session:
            preference = await get_inference_privacy_preference(session, user_model.id)
        if callback_data.preference_revision != preference.revision:
            return await _show_stale_inference_privacy_panel(
                query,
                message,
                preference,
            )
        return await _show_current_inference_privacy_review(
            query,
            message,
            preference_revision=preference.revision,
            terms_changed=True,
        )
    try:
        async with db.session() as session:
            preference = await accept_non_zdr_free_inference(
                session,
                user_model.id,
                expected_revision=callback_data.preference_revision,
                tos_version=callback_data.tos_version,
                privacy_version=callback_data.privacy_version,
            )
    except InferencePrivacyRevisionConflictError as exc:
        return await _show_stale_inference_privacy_panel(
            query,
            message,
            exc.current,
        )
    text, markup = build_inference_privacy_panel(preference)
    await message.edit_text(text, reply_markup=markup)
    logfire.info(
        "inference_privacy_preference_changed",
        mode=preference.mode.value,
        preference_revision=preference.revision,
    )
    await query.answer(
        _("Free models are now allowed in private chat and inline mode.")
    )


@router.callback_query(
    InferencePrivacyCallback.filter(F.action == InferencePrivacyAction.REVOKE)
)
async def revoke_inference_privacy_terms(
    query: CallbackQuery,
    callback_data: InferencePrivacyCallback,
    db: DatabaseManager,
    user_model: UserModel | None,
) -> None:
    """Return to private-only inference with one idempotent action."""
    if (message := await _private_inference_privacy_message(query, user_model)) is None:
        return
    try:
        async with db.session() as session:
            preference = await revoke_non_zdr_free_inference(
                session,
                user_model.id,
                expected_revision=callback_data.preference_revision,
            )
    except InferencePrivacyRevisionConflictError as exc:
        return await _show_stale_inference_privacy_panel(
            query,
            message,
            exc.current,
        )
    text, markup = build_inference_privacy_panel(preference)
    await message.edit_text(text, reply_markup=markup)
    logfire.info(
        "inference_privacy_preference_changed",
        mode=preference.mode.value,
        preference_revision=preference.revision,
    )
    await query.answer(_("Private models only."))


async def _managed_group_free_model_message(
    query: CallbackQuery,
    bot: Bot,
    chat_model: ChatModel | None,
    user_model: UserModel | None,
) -> Message | None:
    message = query.message
    if (
        not isinstance(message, Message)
        or message.chat.type not in {"group", "supergroup"}
        or not chat_model
        or chat_model.type not in {"group", "supergroup"}
        or not user_model
        or user_model.telegram_id != query.from_user.id
    ):
        await query.answer(
            _("This setting is only available to chat admins."),
            show_alert=True,
        )
        return None
    if not await actor_can_manage(bot, message, query.from_user.id):
        await query.answer(
            _("Only chat admins can change this"),
            show_alert=True,
        )
        return None
    return message


def _current_chat_free_model_legal_versions(
    callback_data: ChatFreeModelCallback,
) -> bool:
    return (
        callback_data.tos_version == FREE_INFERENCE_TOS_VERSION
        and callback_data.privacy_version == FREE_INFERENCE_PRIVACY_VERSION
    )


def _apply_chat_free_model_policy(
    chat_model: ChatModel,
    policy: ChatFreeModelPolicy,
) -> None:
    chat_model.free_inference_enabled = policy.enabled
    chat_model.free_inference_revision = policy.revision
    chat_model.free_inference_tos_version = policy.accepted_tos_version
    chat_model.free_inference_privacy_version = policy.accepted_privacy_version
    chat_model.free_inference_accepted_by_user_id = policy.accepted_by_user_id
    chat_model.free_inference_accepted_at = policy.accepted_at
    chat_model.free_inference_revoked_at = policy.revoked_at


async def _show_stale_chat_free_model_panel(
    query: CallbackQuery,
    message: Message,
    bot: Bot,
    chat_model: ChatModel,
    policy: ChatFreeModelPolicy,
) -> None:
    _apply_chat_free_model_policy(chat_model, policy)
    available = await ambient_delivery_available(bot, message.chat.id)
    text, markup = build_context_panel(
        chat_model,
        ambient_available=available,
        can_manage=True,
    )
    await message.edit_text(text, reply_markup=markup)
    await query.answer(
        _("This free-model button expired. Open the setting again."),
        show_alert=True,
    )


@router.callback_query(
    ChatFreeModelCallback.filter(F.action == ChatFreeModelAction.REVIEW)
)
async def review_chat_free_model_terms(
    query: CallbackQuery,
    callback_data: ChatFreeModelCallback,
    db: DatabaseManager,
    bot: Bot,
    chat_model: ChatModel | None,
    user_model: UserModel | None,
) -> None:
    """Place current legal details before a chat administrator's choice."""
    message = await _managed_group_free_model_message(
        query,
        bot,
        chat_model,
        user_model,
    )
    if message is None or chat_model is None:
        return
    async with db.read_session() as session:
        policy = await get_chat_free_model_policy(session, chat_model.id)
    if callback_data.policy_revision != policy.revision:
        return await _show_stale_chat_free_model_panel(
            query,
            message,
            bot,
            chat_model,
            policy,
        )
    text, markup = build_chat_free_model_review(policy.revision)
    await message.edit_text(text, reply_markup=markup)
    if not _current_chat_free_model_legal_versions(callback_data):
        await query.answer(
            _("The terms changed. Review the latest versions."),
            show_alert=True,
        )
        return
    await query.answer()


@router.callback_query(
    ChatFreeModelCallback.filter(F.action == ChatFreeModelAction.ACCEPT)
)
async def accept_chat_free_model_terms(
    query: CallbackQuery,
    callback_data: ChatFreeModelCallback,
    db: DatabaseManager,
    bot: Bot,
    chat_model: ChatModel | None,
    user_model: UserModel | None,
) -> None:
    """Enable chat-wide free fallback after live administrator acceptance."""
    message = await _managed_group_free_model_message(
        query,
        bot,
        chat_model,
        user_model,
    )
    if message is None or chat_model is None or user_model is None:
        return
    if not _current_chat_free_model_legal_versions(callback_data):
        async with db.read_session() as session:
            policy = await get_chat_free_model_policy(session, chat_model.id)
        if callback_data.policy_revision != policy.revision:
            return await _show_stale_chat_free_model_panel(
                query,
                message,
                bot,
                chat_model,
                policy,
            )
        text, markup = build_chat_free_model_review(policy.revision)
        await message.edit_text(text, reply_markup=markup)
        return await query.answer(
            _("The terms changed. Review the latest versions."),
            show_alert=True,
        )
    try:
        async with db.session() as session:
            policy = await enable_chat_non_zdr_free_inference(
                session,
                chat_model.id,
                accepted_by_user_id=user_model.id,
                expected_revision=callback_data.policy_revision,
                tos_version=callback_data.tos_version,
                privacy_version=callback_data.privacy_version,
            )
    except ChatFreeModelRevisionConflictError as exc:
        return await _show_stale_chat_free_model_panel(
            query,
            message,
            bot,
            chat_model,
            exc.current,
        )

    _apply_chat_free_model_policy(chat_model, policy)
    available = await ambient_delivery_available(bot, message.chat.id)
    text, markup = build_context_panel(
        chat_model,
        ambient_available=available,
        can_manage=True,
    )
    await message.edit_text(text, reply_markup=markup)
    await message.answer(
        _(
            "<b>Free models are on</b>\n"
            "When chat credits are unavailable, Derp may send prompts and replies "
            "to OpenRouter and selected free-model providers. Those providers may "
            "store them under their policies. Admins can change this in /settings."
        )
    )
    logfire.info(
        "chat_free_model_policy_changed",
        chat_id=message.chat.id,
        enabled=True,
        policy_revision=policy.revision,
    )
    await query.answer(_("Free models are on for this chat."))


@router.callback_query(
    ChatFreeModelCallback.filter(F.action == ChatFreeModelAction.REVOKE)
)
async def revoke_chat_free_model_terms(
    query: CallbackQuery,
    callback_data: ChatFreeModelCallback,
    db: DatabaseManager,
    bot: Bot,
    chat_model: ChatModel | None,
    user_model: UserModel | None,
) -> None:
    """Disable future chat-wide free fallback after live admin authorization."""
    message = await _managed_group_free_model_message(
        query,
        bot,
        chat_model,
        user_model,
    )
    if message is None or chat_model is None:
        return
    try:
        async with db.session() as session:
            policy = await revoke_chat_non_zdr_free_inference(
                session,
                chat_model.id,
                expected_revision=callback_data.policy_revision,
            )
    except ChatFreeModelRevisionConflictError as exc:
        return await _show_stale_chat_free_model_panel(
            query,
            message,
            bot,
            chat_model,
            exc.current,
        )
    _apply_chat_free_model_policy(chat_model, policy)
    available = await ambient_delivery_available(bot, message.chat.id)
    text, markup = build_context_panel(
        chat_model,
        ambient_available=available,
        can_manage=True,
    )
    await message.edit_text(text, reply_markup=markup)
    logfire.info(
        "chat_free_model_policy_changed",
        chat_id=message.chat.id,
        enabled=False,
        policy_revision=policy.revision,
    )
    await query.answer(_("Free models are off for this chat."))


@router.callback_query(F.data.startswith("cfm:"))
async def reject_stale_chat_free_model_callback(query: CallbackQuery) -> None:
    """Consume malformed or obsolete chat free-model controls."""
    await query.answer(
        _("This free-model button expired. Open the setting again."),
        show_alert=True,
    )


@router.callback_query(F.data.startswith("ifp:"))
async def reject_stale_inference_privacy_callback(query: CallbackQuery) -> None:
    """Consume malformed or obsolete preference controls."""
    await query.answer(
        _("This model privacy button expired. Open the setting again."),
        show_alert=True,
    )


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
        label=_("Delete your saved messages from my memory"),
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
        return await query.answer(_("History is unavailable"), show_alert=True)
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
    await query.answer(
        _(
            "Deleted {count} saved message from my memory",
            "Deleted {count} saved messages from my memory",
            removed,
        ).format(count=removed)
    )


@router.callback_query(ContextCallback.filter(F.action == ContextAction.CLEAR_CONFIRM))
async def confirm_clear_history(query: CallbackQuery, bot: Bot) -> None:
    """Revalidate admin authority before showing the scope purge confirmation."""
    if not isinstance(query.message, Message):
        return await query.answer()
    if not await actor_can_manage(bot, query.message, query.from_user.id):
        return await query.answer(
            _("Only chat admins can clear history"), show_alert=True
        )
    scope = (
        _("topic")
        if query.message.message_thread_id is not None
        else _("whole chat")
        if query.message.chat.is_forum
        else _("chat")
    )
    text, markup = build_destructive_confirmation(
        action=ContextAction.CLEAR,
        label=_("Clear saved history for this {scope}").format(scope=scope),
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
        return await query.answer(_("History is unavailable"), show_alert=True)
    if not await actor_can_manage(bot, query.message, query.from_user.id):
        return await query.answer(
            _("Only chat admins can clear history"), show_alert=True
        )
    async with db.session() as session:
        removed = await clear_history_scope(
            session,
            chat_telegram_id=query.message.chat.id,
            thread_id=query.message.message_thread_id,
            all_threads=bool(
                query.message.chat.is_forum and query.message.message_thread_id is None
            ),
        )
    text, markup = build_privacy_panel(
        chat_model,
        can_manage=True,
        thread_id=query.message.message_thread_id,
    )
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer(
        _(
            "Deleted {count} saved message from my memory",
            "Deleted {count} saved messages from my memory",
            removed,
        ).format(count=removed)
    )


@router.callback_query(
    ContextCallback.filter(F.action == ContextAction.FORGET_FACTS_CONFIRM)
)
async def confirm_forget_shared_facts(query: CallbackQuery, bot: Bot) -> None:
    """Require live admin authority and confirmation before fact deletion."""
    if not isinstance(query.message, Message):
        return await query.answer()
    if not await actor_can_manage(bot, query.message, query.from_user.id):
        return await query.answer(
            _("Only chat admins can forget shared facts"),
            show_alert=True,
        )
    scope = (
        _("topic")
        if query.message.message_thread_id is not None
        else _("whole chat")
        if query.message.chat.is_forum
        else _("chat")
    )
    text, markup = build_destructive_confirmation(
        action=ContextAction.FORGET_FACTS,
        label=_("Forget approved facts in this {scope}").format(scope=scope),
        detail=_(
            "This deletes approved facts here. Saved conversation history stays "
            "unchanged."
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
        return await query.answer(_("Shared facts are unavailable"), show_alert=True)
    if not await actor_can_manage(bot, query.message, query.from_user.id):
        return await query.answer(
            _("Only chat admins can forget shared facts"),
            show_alert=True,
        )
    async with db.session() as session:
        removed = await forget_approved_shared_facts(
            session,
            chat_id=chat_model.id,
            thread_id=query.message.message_thread_id,
            all_threads=bool(
                query.message.chat.is_forum and query.message.message_thread_id is None
            ),
        )
    text, markup = build_privacy_panel(
        chat_model,
        can_manage=True,
        thread_id=query.message.message_thread_id,
    )
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer(
        _(
            "Forgot {count} approved fact",
            "Forgot {count} approved facts",
            removed,
        ).format(count=removed)
    )


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
        return await query.answer(_("This proposal is unavailable"), show_alert=True)
    is_admin = await actor_can_manage(bot, query.message, query.from_user.id)
    if not is_admin and not chat_model.shared_facts_member_edit:
        return await query.answer(
            _("A chat admin must review this fact"), show_alert=True
        )
    try:
        fact_id = UUID(callback_data.fact_id)
    except ValueError:
        return await query.answer(
            _("This proposal is no longer valid"), show_alert=True
        )
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
    except LookupError, SharedFactDecisionConflictError:
        return await query.answer(
            _("This fact was already reviewed or is no longer available"),
            show_alert=True,
        )

    state = (
        _("Approved")
        if callback_data.action is SharedFactAction.APPROVE
        else _("Rejected")
    )
    await query.message.edit_text(
        _("<b>{state} shared fact</b>\n<blockquote>{fact}</blockquote>").format(
            state=state,
            fact=html.quote(fact.fact_text),
        )
    )
    await query.answer(state)


@router.callback_query(ContextCallback.filter(F.action == ContextAction.TOGGLE_CONFIRM))
async def confirm_disable_context(
    query: CallbackQuery,
    bot: Bot,
    chat_model: ChatModel | None,
) -> None:
    """Require a clear whole-chat confirmation before ambient history is purged."""
    if not isinstance(query.message, Message) or not chat_model:
        return await query.answer(_("Settings are unavailable"), show_alert=True)
    if not await actor_can_manage(bot, query.message, query.from_user.id):
        return await query.answer(
            _("Only chat admins can change this"), show_alert=True
        )
    if not chat_model.ambient_history_enabled:
        available = await ambient_delivery_available(bot, query.message.chat.id)
        text, markup = build_context_panel(
            chat_model,
            ambient_available=available,
            can_manage=True,
        )
        await query.message.edit_text(text, reply_markup=markup)
        return await query.answer(_("Context is already off"))
    text, markup = build_ambient_cleanup_confirmation()
    await query.message.edit_text(text, reply_markup=markup)
    await query.answer()


@router.callback_query(
    ContextCallback.filter(
        F.action.in_({ContextAction.TOGGLE, ContextAction.CLEAN_AMBIENT})
    )
)
async def toggle_context(
    query: CallbackQuery,
    callback_data: ContextCallback,
    db: DatabaseManager,
    bot: Bot,
    chat_model: ChatModel | None,
) -> None:
    """Enable or disable ambient capture after live admin authorization."""
    if not isinstance(query.message, Message) or not chat_model:
        return await query.answer(_("Settings are unavailable"), show_alert=True)
    if not await actor_can_manage(bot, query.message, query.from_user.id):
        return await query.answer(
            _("Only chat admins can change this"), show_alert=True
        )
    if callback_data.action is ContextAction.TOGGLE and callback_data.value == 0:
        return await confirm_disable_context(query, bot, chat_model)
    if (callback_data.action is ContextAction.TOGGLE and callback_data.value != 1) or (
        callback_data.action is ContextAction.CLEAN_AMBIENT and callback_data.value != 0
    ):
        return await query.answer(
            _("This setting is no longer valid"),
            show_alert=True,
        )
    enable = callback_data.action is ContextAction.TOGGLE
    available = query.message.chat.type == "private" or (
        await ambient_delivery_available(bot, query.message.chat.id)
    )
    if enable and not available:
        return await query.answer(
            _("Telegram only sends me mentions right now"),
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
        _("Context is on")
        if enable
        else _(
            "Context is off · {count} saved message removed from my memory",
            "Context is off · {count} saved messages removed from my memory",
            purged,
        ).format(count=purged)
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
        return await query.answer(_("Settings are unavailable"), show_alert=True)
    if not await actor_can_manage(bot, query.message, query.from_user.id):
        return await query.answer(
            _("Only chat admins can change this"), show_alert=True
        )
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
    await query.answer(
        _(
            "Messages will be kept for {days} day",
            "Messages will be kept for {days} days",
            callback_data.value,
        ).format(days=callback_data.value)
    )


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
        return await query.answer(_("Settings are unavailable"), show_alert=True)
    if not await actor_can_manage(bot, query.message, query.from_user.id):
        return await query.answer(
            _("Only chat admins can change this"), show_alert=True
        )
    if callback_data.value not in {0, 1}:
        return await query.answer(_("This setting is no longer valid"), show_alert=True)
    flag = _POLICY_FLAGS[callback_data.action]
    if chat_model.type == "private" and flag is not ChatPolicyFlag.EXPENSIVE_TOOLS:
        return await query.answer(
            _("This setting only applies in groups"), show_alert=True
        )
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
    await query.answer(_policy_flag_notice(flag, enabled=enabled))


@router.callback_query(ContextCallback.filter(F.action == ContextAction.ADMIN_POLICY))
async def request_admin_policy(
    query: CallbackQuery,
    bot: Bot,
) -> None:
    """Request one bounded trusted policy paragraph through a native reply."""
    if not isinstance(query.message, Message):
        return await query.answer()
    if not await actor_can_manage(bot, query.message, query.from_user.id):
        return await query.answer(
            _("Only chat admins can set instructions"), show_alert=True
        )
    await query.message.answer(
        admin_policy_prompt(),
        reply_markup=ForceReply(
            selective=True,
            input_field_placeholder=_("One short instruction"),
        ),
    )
    await query.answer()


@router.message(AdminPolicyReplyFilter())
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
        await message.reply(_("Only chat admins can set instructions."))
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
    except ValueError:
        await message.reply(_("Keep the instruction under 2,048 characters."))
        return
    chat_model.admin_policy = stored
    await message.reply(
        _("Chat instructions saved.") if stored else _("Chat instructions cleared.")
    )


@router.message(Command("forget"))
async def forget_replied_message(message: Message, db: DatabaseManager) -> None:
    """Precisely anonymize one replied-to message owned by the requester."""
    if not message.from_user or not message.reply_to_message:
        await message.reply(_("Reply to one of your own messages with /forget."))
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
            _("That message isn't saved here or doesn't belong to you.")
        )
        return
    await message.reply(
        _(
            "Deleted Derp's saved copy. The Telegram message and copies made by "
            "other people remain."
        )
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
            _(
                "Derp uses recent messages for follow-up answers. Open /settings "
                "to view history and deletion settings."
            )
        )


__all__ = [
    "ChatFreeModelAction",
    "ChatFreeModelCallback",
    "ContextAction",
    "ContextCallback",
    "FREE_INFERENCE_PRIVACY_VERSION",
    "FREE_INFERENCE_TOS_VERSION",
    "InferencePrivacyAction",
    "InferencePrivacyCallback",
    "actor_can_manage",
    "ambient_delivery_available",
    "build_ambient_cleanup_confirmation",
    "build_chat_free_model_review",
    "build_creation_panel",
    "build_credit_panel",
    "build_context_panel",
    "build_destructive_confirmation",
    "build_free_model_recovery",
    "build_inference_privacy_panel",
    "build_inference_privacy_review",
    "build_privacy_panel",
    "ensure_group_context_notice",
    "router",
    "show_privacy_controls",
]
