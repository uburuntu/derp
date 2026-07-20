"""Context onboarding and settings stay native, truthful, and authorized."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery, ChatMemberAdministrator, User

from derp.execution import Feature
from derp.handlers.context_settings import (
    ContextAction,
    ContextCallback,
    ambient_delivery_available,
    build_context_panel,
    build_credit_panel,
    build_privacy_panel,
    change_policy_flag,
    delete_my_history,
    ensure_group_context_notice,
    forget_replied_message,
    review_shared_fact_proposal,
    save_admin_policy,
    toggle_context,
    toggle_personal_spend,
)
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
    assert markup.inline_keyboard[0][1].text == "Context: Mentions only"


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


def test_privacy_panel_exposes_personal_deletion_to_non_admin(
    mock_chat_model,
) -> None:
    text, markup = build_privacy_panel(mock_chat_model(), can_manage=False)

    assert "remove your own stored messages" in text
    labels = [button.text for row in markup.inline_keyboard for button in row]
    assert "Delete my messages" in labels
    assert "Clear this chat" not in labels


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

    assert "Monthly allowance: 12" in text
    assert "renews 19 Aug 2026" in text
    assert "Purchased: 34" in text
    assert "Payment debt: 7" in text
    assert "Image generation refund: +5" in text
    assert "Shared purchased: 56 · paused by admins" in text
    assert markup.inline_keyboard[0][0].text == "Personal fallback: Always"


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
    ledger.personal_consent_enabled = AsyncMock(return_value=True)
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
        ContextCallback(action=ContextAction.PERSONAL_SPEND, value=1),
        ledger,
        user,
        chat,
    )

    ledger.grant_personal_consent.assert_awaited_once_with(user.id, chat.id)
    ledger.revoke_personal_consent.assert_not_awaited()
    assert "Personal fallback updated" in query.answer.await_args.args[0]


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
    callback = ContextCallback(action=ContextAction.TOGGLE, value=0)

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
    assert "removed 4 messages" in query.answer.await_args.args[0]
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


@pytest.mark.asyncio
async def test_personal_deletion_uses_callback_actor_identity(
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
    session = MagicMock()
    db = MagicMock()
    db.session.return_value.__aenter__ = AsyncMock(return_value=session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "derp.handlers.context_settings.tombstone_user_messages",
            new=AsyncMock(return_value=3),
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
    assert "Removed 3" in query.answer.await_args.args[0]


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
    assert "does not belong to you" in command.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_policy_flag_requires_live_admin_and_updates_exact_field(
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
    chat = mock_chat_model(expensive_tools_enabled=True)
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
            ContextCallback(action=ContextAction.EXPENSIVE_TOOLS, value=0),
            db,
            MagicMock(spec=Bot),
            chat,
        )

    set_flag.assert_awaited_once_with(
        session,
        chat_telegram_id=message.chat.id,
        flag=ChatPolicyFlag.EXPENSIVE_TOOLS,
        enabled=False,
    )
    assert chat.expensive_tools_enabled is False


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
