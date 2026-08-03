"""Context onboarding and settings stay native, truthful, and authorized."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery, ChatMemberAdministrator, User

from derp.common.legal_documents import LegalDocumentKind
from derp.execution import Feature
from derp.handlers.context_settings import (
    AdminPolicyReplyFilter,
    ContextAction,
    ContextCallback,
    admin_policy_prompt,
    ambient_delivery_available,
    build_context_panel,
    build_creation_panel,
    build_credit_panel,
    build_privacy_panel,
    change_policy_flag,
    delete_my_history,
    ensure_group_context_notice,
    forget_replied_message,
    review_shared_fact_proposal,
    save_admin_policy,
    show_creation_menu,
    show_credit_menu,
    show_privacy_controls,
    toggle_context,
    toggle_personal_spend,
)
from derp.handlers.legal_support import LegalDocumentCallback
from derp.history.policy import ChatPolicyFlag
from derp.operations import (
    WalletActivity,
    WalletActivityKind,
    WalletBalance,
    WalletOwner,
    WalletOwnerKind,
    WalletStatement,
)
from derp.tools.shared_facts import SharedFactAction, SharedFactCallback


def test_panel_never_claims_ambient_context_when_telegram_cannot_deliver(
    mock_chat_model,
) -> None:
    chat = mock_chat_model(ambient_history_enabled=True, retention_days=30)

    text, markup = build_context_panel(
        chat,
        ambient_available=False,
        can_manage=True,
    )

    assert "Context: Mentions only" in text
    assert markup.inline_keyboard[0][1].text == "Turn context on"


def test_private_panel_reports_always_on_history_without_ambient_toggle(
    mock_chat_model,
) -> None:
    chat = mock_chat_model(chat_type="private", ambient_history_enabled=False)

    text, markup = build_context_panel(
        chat,
        ambient_available=True,
        can_manage=True,
    )

    assert "History: On · 30 days" in text
    callback = ContextCallback.unpack(markup.inline_keyboard[0][1].callback_data)
    assert callback.action is ContextAction.PRIVACY


@pytest.mark.parametrize(
    ("enabled", "expected_actions"),
    [
        (
            True,
            {
                "Turn context off",
                "Require admin fact review",
                "Pause chat credits",
                "Disable paid features",
            },
        ),
        (
            False,
            {
                "Turn context on",
                "Let members edit facts",
                "Enable chat credits",
                "Enable paid features",
            },
        ),
    ],
)
def test_admin_settings_buttons_describe_the_next_action(
    mock_chat_model,
    enabled: bool,
    expected_actions: set[str],
) -> None:
    chat = mock_chat_model(
        ambient_history_enabled=enabled,
        shared_facts_member_edit=enabled,
        shared_credit_spending_enabled=enabled,
        expensive_tools_enabled=enabled,
    )

    _, markup = build_context_panel(
        chat,
        ambient_available=True,
        can_manage=True,
    )

    labels = {button.text for row in markup.inline_keyboard for button in row}
    assert expected_actions <= labels
    assert all("tools" not in label.casefold() for label in labels)


def test_context_panel_has_a_first_class_creation_entry(mock_chat_model) -> None:
    _, markup = build_context_panel(
        mock_chat_model(),
        ambient_available=True,
        can_manage=False,
    )

    buttons = [button for row in markup.inline_keyboard for button in row]
    creation = next(button for button in buttons if button.text == "Create")
    assert creation.callback_data is not None
    callback = ContextCallback.unpack(creation.callback_data)
    assert callback.action is ContextAction.CREATION
    personal_spending = next(
        button for button in buttons if button.text == "Change how my credits are used"
    )
    assert personal_spending.callback_data is not None
    spending_callback = ContextCallback.unpack(personal_spending.callback_data)
    assert spending_callback.action is ContextAction.PERSONAL_SPEND
    assert spending_callback.value == -1


def test_creation_panel_exposes_only_durable_creation_commands() -> None:
    text, markup = build_creation_panel()

    assert "<code>/imagine</code>" in text
    assert "<code>/edit</code>" in text
    assert "<code>/tts</code>" in text
    assert "/think" not in text
    assert "/video" not in text
    back = markup.inline_keyboard[0][0]
    assert back.callback_data is not None
    callback = ContextCallback.unpack(back.callback_data)
    assert callback.action is ContextAction.MENU


@pytest.mark.asyncio
async def test_creation_callback_edits_the_existing_panel(make_message) -> None:
    message = make_message(text="panel", chat_type="supergroup")
    message.edit_text = AsyncMock()
    query = MagicMock(spec=CallbackQuery)
    query.message = message
    query.answer = AsyncMock()

    await show_creation_menu(query)

    text, markup = build_creation_panel()
    message.edit_text.assert_awaited_once_with(text, reply_markup=markup)
    query.answer.assert_awaited_once_with()


def test_privacy_panel_exposes_personal_deletion_to_non_admin(
    mock_chat_model,
) -> None:
    text, markup = build_privacy_panel(mock_chat_model(), can_manage=False)

    assert "delete your own saved messages from my memory" in text
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "Delete from my memory" in labels
    assert "Clear this chat" not in labels
    assert "Contact support" in labels
    documents = {
        button.text: LegalDocumentCallback.unpack(button.callback_data).document
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("legal:")
    }
    assert documents == {
        "Privacy policy": LegalDocumentKind.PRIVACY,
        "Terms of use": LegalDocumentKind.TERMS,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["private", "supergroup"])
async def test_privacy_command_opens_scoped_controls_directly(
    make_message,
    mock_chat_model,
    chat_type: str,
) -> None:
    message = make_message(text="/privacy", chat_type=chat_type)
    message.reply = AsyncMock()
    bot = MagicMock(spec=Bot)
    bot.get_chat_administrators = AsyncMock(return_value=[])
    chat = mock_chat_model(chat_type=chat_type)
    can_manage = chat_type == "private"

    with patch(
        "derp.handlers.context_settings.actor_can_manage",
        new=AsyncMock(return_value=can_manage),
    ):
        await show_privacy_controls(message, bot, chat)

    text, markup = build_privacy_panel(
        chat,
        can_manage=can_manage,
        thread_id=message.message_thread_id,
    )
    message.reply.assert_awaited_once_with(text, reply_markup=markup)


def test_credit_panel_shows_inventories_debt_and_personal_preference() -> None:
    personal = WalletStatement(
        WalletBalance(
            WalletOwner(WalletOwnerKind.USER, UUID(int=1)),
            allowance_available=12,
            purchased_available=34,
            reserved=5,
            consumed=6,
            debt=7,
        ),
        allowance_period_end=datetime(2026, 8, 19, tzinfo=UTC),
        renewal_enabled=True,
        recent_activity=(
            WalletActivity(
                WalletActivityKind.REFUND,
                5,
                datetime(2026, 7, 20, tzinfo=UTC),
                Feature.IMAGE_GENERATE,
            ),
        ),
    )
    shared = WalletStatement(
        WalletBalance(
            WalletOwner(WalletOwnerKind.CHAT, UUID(int=2)),
            allowance_available=0,
            purchased_available=56,
            reserved=8,
            consumed=9,
            debt=0,
        )
    )

    text, markup = build_credit_panel(
        personal,
        shared=shared,
        shared_spending_enabled=False,
        personal_fallback_enabled=True,
    )

    assert "Monthly plan: 12 credits" in text
    assert "renews 19 Aug 2026" in text
    assert "Purchased: 34 credits" in text
    assert "Payment debt: 7 credits" in text
    assert text.count("Reserved:") == 2
    assert "Image generation refund: +5 credits" in text
    assert "Purchased: 56 credits · paused by admins" in text
    assert markup.inline_keyboard[0][0].text == "Ask before using my credits"


def test_credit_panel_localizes_dates_in_russian(setup_i18n) -> None:
    personal = WalletStatement(
        WalletBalance(
            WalletOwner(WalletOwnerKind.USER, UUID(int=1)),
            allowance_available=12,
            purchased_available=0,
            reserved=0,
            consumed=0,
            debt=0,
        ),
        allowance_period_end=datetime(2026, 8, 19, tzinfo=UTC),
        renewal_enabled=True,
        recent_activity=(
            WalletActivity(
                WalletActivityKind.CHARGE,
                1,
                datetime(2026, 7, 20, tzinfo=UTC),
                Feature.CHAT,
            ),
        ),
    )

    with setup_i18n.use_locale("ru"):
        text, _ = build_credit_panel(
            personal,
            shared=None,
            shared_spending_enabled=False,
            personal_fallback_enabled=False,
        )

    assert "19 авг. 2026" in text
    assert "20 июл." in text


@pytest.mark.parametrize(
    ("enabled", "expected"),
    [
        (True, "Ask before using my credits"),
        (False, "Use my credits without asking"),
    ],
)
def test_credit_permission_button_describes_the_next_action(
    enabled: bool,
    expected: str,
) -> None:
    personal = WalletStatement(
        WalletBalance(WalletOwner(WalletOwnerKind.USER, UUID(int=1)), 0, 0, 0, 0, 0)
    )
    shared = WalletStatement(
        WalletBalance(WalletOwner(WalletOwnerKind.CHAT, UUID(int=2)), 0, 0, 0, 0, 0)
    )

    _, markup = build_credit_panel(
        personal,
        shared=shared,
        shared_spending_enabled=True,
        personal_fallback_enabled=enabled,
    )

    assert markup.inline_keyboard[0][0].text == expected


@pytest.mark.parametrize(
    ("credits", "expected"),
    [(1, "Purchased: 1 credit"), (2, "Purchased: 2 credits")],
)
def test_credit_panel_pluralizes_credit_counts(credits: int, expected: str) -> None:
    personal = WalletStatement(
        WalletBalance(
            WalletOwner(WalletOwnerKind.USER, UUID(int=1)),
            allowance_available=credits,
            purchased_available=credits,
            reserved=0,
            consumed=0,
            debt=0,
        )
    )

    text, _ = build_credit_panel(
        personal,
        shared=None,
        shared_spending_enabled=False,
        personal_fallback_enabled=False,
    )

    assert expected in text


@pytest.mark.parametrize(
    ("days", "expected"),
    [(1, "after 1 day"), (2, "after 2 days")],
)
def test_privacy_panel_pluralizes_retention_days(
    mock_chat_model,
    days: int,
    expected: str,
) -> None:
    text, _ = build_privacy_panel(
        mock_chat_model(retention_days=days),
        can_manage=False,
    )

    assert expected in text


@pytest.mark.asyncio
async def test_personal_fallback_toggle_is_scoped_to_callback_actor(
    make_message,
    make_user,
    mock_chat_model,
    mock_user_model,
) -> None:
    message = make_message(text="panel", chat_type="supergroup")
    message.edit_text = AsyncMock()
    query = MagicMock(spec=CallbackQuery)
    query.message = message
    query.from_user = make_user(id=42)
    query.answer = AsyncMock()
    user = mock_user_model(user_id=UUID(int=1), telegram_id=42)
    chat = mock_chat_model(chat_id=UUID(int=2))
    ledger = MagicMock()
    ledger.grant_personal_consent = AsyncMock()
    ledger.revoke_personal_consent = AsyncMock()
    ledger.personal_consent_enabled = AsyncMock(return_value=False)
    ledger.statement = AsyncMock(
        side_effect=[
            WalletStatement(
                WalletBalance(
                    WalletOwner(WalletOwnerKind.USER, user.id), 0, 10, 0, 0, 0
                )
            ),
            WalletStatement(
                WalletBalance(
                    WalletOwner(WalletOwnerKind.CHAT, chat.id), 0, 20, 0, 0, 0
                )
            ),
        ]
    )

    await toggle_personal_spend(
        query,
        ContextCallback(action=ContextAction.PERSONAL_SPEND, value=-1),
        ledger,
        user,
        chat,
    )

    ledger.grant_personal_consent.assert_awaited_once_with(user.id, chat.id)
    ledger.revoke_personal_consent.assert_not_awaited()
    ledger.personal_consent_enabled.assert_awaited_once_with(user.id, chat.id)
    query.answer.assert_awaited_once_with(
        "Your credits can be used here without asking",
        show_alert=True,
    )
    message.edit_text.assert_not_awaited()
    ledger.statement.assert_not_awaited()


@pytest.mark.asyncio
async def test_personal_credit_toggle_confirms_ask_first_state(
    make_message,
    make_user,
    mock_chat_model,
    mock_user_model,
) -> None:
    query = MagicMock(spec=CallbackQuery)
    query.message = make_message(text="panel", chat_type="supergroup")
    query.from_user = make_user(id=42)
    query.answer = AsyncMock()
    user = mock_user_model(user_id=UUID(int=1), telegram_id=42)
    chat = mock_chat_model(chat_id=UUID(int=2))
    ledger = MagicMock()
    ledger.grant_personal_consent = AsyncMock()
    ledger.revoke_personal_consent = AsyncMock()

    await toggle_personal_spend(
        query,
        ContextCallback(action=ContextAction.PERSONAL_SPEND, value=0),
        ledger,
        user,
        chat,
    )

    ledger.revoke_personal_consent.assert_awaited_once_with(user.id, chat.id)
    ledger.grant_personal_consent.assert_not_awaited()
    query.answer.assert_awaited_once_with(
        "I'll ask before using your credits here",
        show_alert=True,
    )


@pytest.mark.asyncio
async def test_group_credit_callback_keeps_personal_balances_actor_only(
    make_message,
    make_user,
    mock_chat_model,
    mock_user_model,
) -> None:
    message = make_message(text="panel", chat_type="supergroup")
    message.edit_text = AsyncMock()
    query = MagicMock(spec=CallbackQuery)
    query.message = message
    query.from_user = make_user(id=42)
    query.answer = AsyncMock()
    user = mock_user_model(user_id=UUID(int=1), telegram_id=42)
    chat = mock_chat_model(chat_id=UUID(int=2))
    ledger = MagicMock()
    ledger.personal_consent_enabled = AsyncMock(return_value=True)
    ledger.statement = AsyncMock(
        side_effect=[
            WalletStatement(
                WalletBalance(
                    WalletOwner(WalletOwnerKind.USER, user.id), 12, 34, 0, 0, 0
                )
            ),
            WalletStatement(
                WalletBalance(
                    WalletOwner(WalletOwnerKind.CHAT, chat.id), 0, 56, 0, 0, 0
                )
            ),
        ]
    )

    await show_credit_menu(query, ledger, user, chat)

    message.edit_text.assert_not_awaited()
    alert = query.answer.await_args.args[0]
    assert "Monthly plan: 12 credits" in alert
    assert "Purchased: 34 credits" in alert
    assert "This chat: 56 purchased credits" in alert
    assert len(alert) <= 200
    assert query.answer.await_args.kwargs == {"show_alert": True}


@pytest.mark.asyncio
async def test_admin_membership_enables_ambient_delivery_fallback() -> None:
    bot = MagicMock(spec=Bot)
    bot.get_me = AsyncMock(
        return_value=User(
            id=99,
            is_bot=True,
            first_name="Derp",
            can_read_all_group_messages=False,
        )
    )
    bot.get_chat_member = AsyncMock(
        return_value=ChatMemberAdministrator.model_construct(status="administrator")
    )

    assert await ambient_delivery_available(bot, -1001)


@pytest.mark.asyncio
async def test_first_invocation_sends_notice_before_enabling_capture(
    make_message,
    mock_chat_model,
) -> None:
    message = make_message(text="/derp hello", user_id=42)
    chat = mock_chat_model(
        context_notice_version=0,
        ambient_history_enabled=False,
        retention_days=30,
    )
    session = MagicMock()
    db = MagicMock()
    db.session.return_value.__aenter__ = AsyncMock(return_value=session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=None)
    bot = MagicMock(spec=Bot)

    with (
        patch(
            "derp.handlers.context_settings.ambient_delivery_available",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "derp.handlers.context_settings.actor_can_manage",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "derp.handlers.context_settings.acknowledge_context_notice",
            new_callable=AsyncMock,
        ) as acknowledge,
    ):
        sent = await ensure_group_context_notice(
            message,
            chat_model=chat,
            db=db,
            bot=bot,
        )

    assert sent
    message.reply.assert_awaited_once()
    assert "Context: On" in message.reply.await_args.args[0]
    acknowledge.assert_awaited_once_with(
        session,
        chat_telegram_id=message.chat.id,
        ambient_enabled=True,
    )
    assert chat.context_notice_version == 1
    assert chat.ambient_history_enabled is True


@pytest.mark.asyncio
async def test_toggle_requires_live_admin_and_purges_when_disabled(
    make_message,
    make_user,
    mock_chat_model,
) -> None:
    message = make_message(text="panel")
    message.edit_text = AsyncMock()
    query = MagicMock(spec=CallbackQuery)
    query.message = message
    query.from_user = make_user(id=42)
    query.answer = AsyncMock()
    chat = mock_chat_model(ambient_history_enabled=True, retention_days=30)
    session = MagicMock()
    db = MagicMock()
    db.session.return_value.__aenter__ = AsyncMock(return_value=session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=None)
    callback = ContextCallback(action=ContextAction.CLEAN_AMBIENT, value=0)

    with (
        patch(
            "derp.handlers.context_settings.actor_can_manage",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "derp.handlers.context_settings.ambient_delivery_available",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "derp.handlers.context_settings.set_ambient_history",
            new=AsyncMock(return_value=4),
        ) as set_context,
    ):
        await toggle_context(
            query,
            callback,
            db,
            MagicMock(spec=Bot),
            chat,
        )

    set_context.assert_awaited_once_with(
        session,
        chat_telegram_id=message.chat.id,
        enabled=False,
    )
    assert chat.ambient_history_enabled is False
    assert "4 saved messages removed from my memory" in query.answer.await_args.args[0]
    message.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_non_admin_toggle_fails_before_database_access(
    make_message,
    make_user,
    mock_chat_model,
) -> None:
    query = MagicMock(spec=CallbackQuery)
    query.message = make_message(text="panel")
    query.from_user = make_user(id=42)
    query.answer = AsyncMock()
    db = MagicMock()

    with patch(
        "derp.handlers.context_settings.actor_can_manage",
        new=AsyncMock(return_value=False),
    ):
        await toggle_context(
            query,
            ContextCallback(action=ContextAction.TOGGLE, value=1),
            db,
            MagicMock(spec=Bot),
            mock_chat_model(),
        )

    db.session.assert_not_called()
    assert query.answer.await_args.kwargs["show_alert"] is True


@pytest.mark.parametrize(
    ("removed", "expected"),
    [
        (1, "Deleted 1 saved message from my memory"),
        (2, "Deleted 2 saved messages from my memory"),
    ],
)
@pytest.mark.asyncio
async def test_personal_deletion_uses_callback_actor_identity(
    make_message,
    make_user,
    mock_chat_model,
    removed: int,
    expected: str,
) -> None:
    message = make_message(text="panel")
    message.edit_text = AsyncMock()
    query = MagicMock(spec=CallbackQuery)
    query.message = message
    query.from_user = make_user(id=42)
    query.answer = AsyncMock()
    session = MagicMock()
    db = MagicMock()
    db.session.return_value.__aenter__ = AsyncMock(return_value=session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "derp.handlers.context_settings.tombstone_user_messages",
            new=AsyncMock(return_value=removed),
        ) as tombstone,
        patch(
            "derp.handlers.context_settings.actor_can_manage",
            new=AsyncMock(return_value=False),
        ),
    ):
        await delete_my_history(
            query,
            db,
            MagicMock(spec=Bot),
            mock_chat_model(),
        )

    tombstone.assert_awaited_once_with(
        session,
        chat_telegram_id=message.chat.id,
        actor_telegram_id=42,
    )
    assert query.answer.await_args.args[0] == expected


@pytest.mark.asyncio
async def test_forget_targets_replied_message_and_never_trusts_its_sender(
    make_message,
) -> None:
    target = make_message(message_id=77, user_id=999, text="target")
    command = make_message(message_id=78, user_id=42, text="/forget")
    command.reply_to_message = target
    session = MagicMock()
    db = MagicMock()
    db.session.return_value.__aenter__ = AsyncMock(return_value=session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "derp.handlers.context_settings.tombstone_user_messages",
        new=AsyncMock(return_value=0),
    ) as tombstone:
        await forget_replied_message(command, db)

    tombstone.assert_awaited_once_with(
        session,
        chat_telegram_id=command.chat.id,
        actor_telegram_id=42,
        telegram_message_id=77,
    )
    assert "doesn't belong to you" in command.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_admin_policy_reply_filter_matches_current_locale_prompt(
    make_message,
) -> None:
    prompt = make_message(text=admin_policy_prompt())
    message = make_message(text="Keep replies concise.")
    message.reply_to_message = prompt

    assert await AdminPolicyReplyFilter()(message)


@pytest.mark.parametrize(
    ("action", "flag", "field", "enabled", "notice"),
    [
        (
            ContextAction.FACT_MEMBER_EDIT,
            ChatPolicyFlag.SHARED_FACTS_MEMBER_EDIT,
            "shared_facts_member_edit",
            True,
            "Members can now edit shared facts",
        ),
        (
            ContextAction.FACT_MEMBER_EDIT,
            ChatPolicyFlag.SHARED_FACTS_MEMBER_EDIT,
            "shared_facts_member_edit",
            False,
            "Shared facts now need admin review",
        ),
        (
            ContextAction.SHARED_SPEND,
            ChatPolicyFlag.SHARED_CREDIT_SPENDING,
            "shared_credit_spending_enabled",
            True,
            "Chat credits are available",
        ),
        (
            ContextAction.SHARED_SPEND,
            ChatPolicyFlag.SHARED_CREDIT_SPENDING,
            "shared_credit_spending_enabled",
            False,
            "Chat credits are paused",
        ),
        (
            ContextAction.EXPENSIVE_TOOLS,
            ChatPolicyFlag.EXPENSIVE_TOOLS,
            "expensive_tools_enabled",
            True,
            "Paid features are enabled",
        ),
        (
            ContextAction.EXPENSIVE_TOOLS,
            ChatPolicyFlag.EXPENSIVE_TOOLS,
            "expensive_tools_enabled",
            False,
            "Paid features are disabled",
        ),
    ],
)
@pytest.mark.asyncio
async def test_policy_flag_requires_live_admin_and_updates_exact_field(
    make_message,
    make_user,
    mock_chat_model,
    action: ContextAction,
    flag: ChatPolicyFlag,
    field: str,
    enabled: bool,
    notice: str,
) -> None:
    message = make_message(text="panel")
    message.edit_text = AsyncMock()
    query = MagicMock(spec=CallbackQuery)
    query.message = message
    query.from_user = make_user(id=42)
    query.answer = AsyncMock()
    chat = mock_chat_model(**{field: not enabled})
    session = MagicMock()
    db = MagicMock()
    db.session.return_value.__aenter__ = AsyncMock(return_value=session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "derp.handlers.context_settings.actor_can_manage",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "derp.handlers.context_settings.ambient_delivery_available",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "derp.handlers.context_settings.set_chat_policy_flag",
            new_callable=AsyncMock,
        ) as set_flag,
    ):
        await change_policy_flag(
            query,
            ContextCallback(action=action, value=int(enabled)),
            db,
            MagicMock(spec=Bot),
            chat,
        )

    set_flag.assert_awaited_once_with(
        session,
        chat_telegram_id=message.chat.id,
        flag=flag,
        enabled=enabled,
    )
    assert getattr(chat, field) is enabled
    query.answer.assert_awaited_once_with(notice)


@pytest.mark.asyncio
async def test_shared_fact_review_is_exactly_scoped_and_admin_audited(
    make_message,
    make_user,
    mock_chat_model,
    mock_user_model,
) -> None:
    message = make_message(text="proposal", message_thread_id=77)
    message.edit_text = AsyncMock()
    query = MagicMock(spec=CallbackQuery)
    query.message = message
    query.from_user = make_user(id=42)
    query.answer = AsyncMock()
    chat = mock_chat_model(chat_id=UUID(int=1))
    user = mock_user_model(user_id=UUID(int=2))
    session = MagicMock()
    db = MagicMock()
    db.session.return_value.__aenter__ = AsyncMock(return_value=session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=None)
    fact = MagicMock(fact_text="The release is Friday.")

    with (
        patch(
            "derp.handlers.context_settings.actor_can_manage",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "derp.handlers.context_settings.approve_shared_fact",
            new=AsyncMock(return_value=fact),
        ) as approve,
    ):
        await review_shared_fact_proposal(
            query,
            SharedFactCallback(
                action=SharedFactAction.APPROVE,
                fact_id=str(UUID(int=3)),
            ),
            db,
            MagicMock(spec=Bot),
            chat,
            user,
        )

    approve.assert_awaited_once_with(
        session,
        fact_id=UUID(int=3),
        chat_id=UUID(int=1),
        thread_id=77,
        admin_actor_id=UUID(int=2),
    )
    assert "Approved shared fact" in message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_admin_policy_input_is_removed_from_conversation_history(
    make_message,
    mock_chat_model,
) -> None:
    message = make_message(text="Prefer concise replies.", user_id=42)
    chat = mock_chat_model()
    session = MagicMock()
    db = MagicMock()
    db.session.return_value.__aenter__ = AsyncMock(return_value=session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "derp.handlers.context_settings.actor_can_manage",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "derp.handlers.context_settings.set_admin_policy",
            new=AsyncMock(return_value="Prefer concise replies."),
        ) as set_policy,
        patch(
            "derp.handlers.context_settings.remove_disqualified_message",
            new_callable=AsyncMock,
        ) as remove,
    ):
        await save_admin_policy(message, db, MagicMock(spec=Bot), chat)

    set_policy.assert_awaited_once()
    remove.assert_awaited_once_with(
        session,
        chat_telegram_id=message.chat.id,
        telegram_message_id=message.message_id,
    )
    assert chat.admin_policy == "Prefer concise replies."
