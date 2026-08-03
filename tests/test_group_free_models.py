"""Group free-model fallback is explicit, admin-controlled, and visible."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update

from derp.db.inference_privacy import (
    ChatFreeModelRevisionConflictError,
    enable_chat_non_zdr_free_inference,
    revoke_chat_non_zdr_free_inference,
)
from derp.handlers.context_settings import (
    ChatFreeModelAction,
    ChatFreeModelCallback,
    ContextAction,
    ContextCallback,
    InferencePrivacyAction,
    InferencePrivacyCallback,
    accept_chat_free_model_terms,
    build_ambient_cleanup_confirmation,
    build_context_panel,
    build_free_model_recovery,
    confirm_disable_context,
    toggle_context,
)
from derp.inference import (
    FREE_INFERENCE_PRIVACY_VERSION,
    FREE_INFERENCE_TOS_VERSION,
    ChatFreeModelPolicy,
    InferenceContext,
    InferencePrivacyPreference,
    NonZdrFreeInferenceReason,
    decide_non_zdr_free_inference,
)
from derp.models import Chat

ADMIN_ID = UUID(int=1)
ACCEPTED_AT = datetime(2026, 8, 3, 12, tzinfo=UTC)


def _enabled_policy() -> ChatFreeModelPolicy:
    return ChatFreeModelPolicy().enable(
        tos_version=FREE_INFERENCE_TOS_VERSION,
        privacy_version=FREE_INFERENCE_PRIVACY_VERSION,
        accepted_by_user_id=ADMIN_ID,
        accepted_at=ACCEPTED_AT,
    )


def _callback(
    action: ChatFreeModelAction,
    *,
    revision: int = 1,
) -> ChatFreeModelCallback:
    return ChatFreeModelCallback(
        action=action,
        tos_version=FREE_INFERENCE_TOS_VERSION,
        privacy_version=FREE_INFERENCE_PRIVACY_VERSION,
        policy_revision=revision,
    )


def _policy_row(policy: ChatFreeModelPolicy) -> dict[str, object]:
    return {
        "free_inference_enabled": policy.enabled,
        "free_inference_revision": policy.revision,
        "free_inference_tos_version": policy.accepted_tos_version,
        "free_inference_privacy_version": policy.accepted_privacy_version,
        "free_inference_accepted_by_user_id": policy.accepted_by_user_id,
        "free_inference_accepted_at": policy.accepted_at,
        "free_inference_revoked_at": policy.revoked_at,
    }


def _query_result(row: dict[str, object] | None) -> MagicMock:
    result = MagicMock()
    result.mappings.return_value.one_or_none.return_value = row
    return result


def _session(*results: MagicMock) -> MagicMock:
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=results)
    return session


def test_group_policy_not_member_preference_controls_free_fallback() -> None:
    allowed = decide_non_zdr_free_inference(
        InferencePrivacyPreference(),
        context=InferenceContext.SUPERGROUP,
        current_tos_version=FREE_INFERENCE_TOS_VERSION,
        current_privacy_version=FREE_INFERENCE_PRIVACY_VERSION,
        chat_policy=_enabled_policy(),
    )
    revoked_policy = _enabled_policy().revoke(
        revoked_at=ACCEPTED_AT + timedelta(minutes=1)
    )
    blocked = decide_non_zdr_free_inference(
        InferencePrivacyPreference(),
        context=InferenceContext.SUPERGROUP,
        current_tos_version=FREE_INFERENCE_TOS_VERSION,
        current_privacy_version=FREE_INFERENCE_PRIVACY_VERSION,
        chat_policy=revoked_policy,
    )

    assert allowed.allowed
    assert allowed.preference_revision == 2
    assert blocked.reason is NonZdrFreeInferenceReason.CHAT_ADMIN_OPT_IN_REQUIRED
    assert blocked.preference_revision == 3


def test_group_policy_fails_closed_when_legal_versions_change() -> None:
    decision = decide_non_zdr_free_inference(
        InferencePrivacyPreference(),
        context=InferenceContext.GROUP,
        current_tos_version="next-terms",
        current_privacy_version=FREE_INFERENCE_PRIVACY_VERSION,
        chat_policy=_enabled_policy(),
    )

    assert decision.reason is NonZdrFreeInferenceReason.LEGAL_REACCEPTANCE_REQUIRED


def test_chat_schema_defaults_off_and_requires_complete_acceptance() -> None:
    table = Chat.__table__

    assert table.c.free_inference_enabled.server_default.arg.text == "false"
    assert table.c.free_inference_revision.server_default.arg.text == "1"
    assert {
        "chat_free_inference_revision_positive",
        "chat_free_inference_acceptance_complete",
        "chat_free_inference_versions_nonblank",
        "chat_free_inference_state_complete",
        "chat_free_inference_revocation_order",
    } <= {constraint.name for constraint in table.constraints}


@pytest.mark.asyncio
async def test_admin_acceptance_locks_and_persists_complete_chat_policy() -> None:
    session = _session(_query_result(_policy_row(ChatFreeModelPolicy())), MagicMock())

    policy = await enable_chat_non_zdr_free_inference(
        session,
        UUID(int=2),
        accepted_by_user_id=ADMIN_ID,
        expected_revision=1,
        tos_version=FREE_INFERENCE_TOS_VERSION,
        privacy_version=FREE_INFERENCE_PRIVACY_VERSION,
        accepted_at=ACCEPTED_AT,
    )

    assert policy == _enabled_policy()
    select_statement = session.execute.await_args_list[0].args[0]
    update_statement = session.execute.await_args_list[1].args[0]
    assert select_statement._for_update_arg is not None
    assert isinstance(update_statement, Update)
    params = update_statement.compile().params
    assert params["free_inference_enabled"] is True
    assert params["free_inference_revision"] == 2
    assert params["free_inference_accepted_by_user_id"] == ADMIN_ID
    assert params["free_inference_accepted_at"] == ACCEPTED_AT


@pytest.mark.asyncio
async def test_stale_chat_policy_control_cannot_reenable_after_revocation() -> None:
    revoked = _enabled_policy().revoke(revoked_at=ACCEPTED_AT + timedelta(minutes=1))
    session = _session(_query_result(_policy_row(revoked)))

    with pytest.raises(ChatFreeModelRevisionConflictError) as raised:
        await enable_chat_non_zdr_free_inference(
            session,
            UUID(int=2),
            accepted_by_user_id=ADMIN_ID,
            expected_revision=1,
            tos_version=FREE_INFERENCE_TOS_VERSION,
            privacy_version=FREE_INFERENCE_PRIVACY_VERSION,
        )

    assert raised.value.current == revoked
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_policy_revocation_preserves_acceptance_audit() -> None:
    enabled = _enabled_policy()
    revoked_at = ACCEPTED_AT + timedelta(minutes=1)
    session = _session(_query_result(_policy_row(enabled)), MagicMock())

    policy = await revoke_chat_non_zdr_free_inference(
        session,
        UUID(int=2),
        expected_revision=enabled.revision,
        revoked_at=revoked_at,
    )

    assert policy.enabled is False
    assert policy.accepted_by_user_id == ADMIN_ID
    assert policy.accepted_at == ACCEPTED_AT
    assert policy.revoked_at == revoked_at
    params = session.execute.await_args_list[1].args[0].compile().params
    assert params["free_inference_enabled"] is False
    assert params["free_inference_revoked_at"] == revoked_at


def test_group_settings_show_state_and_admin_action(mock_chat_model) -> None:
    chat = mock_chat_model()

    text, markup = build_context_panel(
        chat,
        ambient_available=True,
        can_manage=True,
    )

    assert "Free models: Off" in text
    button = next(
        button
        for row in markup.inline_keyboard
        for button in row
        if button.text == "Review free-model privacy"
    )
    callback = ChatFreeModelCallback.unpack(button.callback_data)
    assert callback.action is ChatFreeModelAction.REVIEW
    assert callback.policy_revision == 1
    assert len(button.callback_data.encode()) <= 64


def test_context_off_button_opens_confirmation_before_cleanup(mock_chat_model) -> None:
    _, markup = build_context_panel(
        mock_chat_model(ambient_history_enabled=True),
        ambient_available=True,
        can_manage=True,
    )

    callback = ContextCallback.unpack(markup.inline_keyboard[0][1].callback_data)
    assert callback.action is ContextAction.TOGGLE_CONFIRM
    assert callback.value == 0


def test_channel_settings_do_not_offer_group_free_policy(mock_chat_model) -> None:
    text, markup = build_context_panel(
        mock_chat_model(chat_type="channel"),
        ambient_available=True,
        can_manage=True,
    )

    labels = {button.text for row in markup.inline_keyboard for button in row}
    assert "Free models:" not in text
    assert "Review free-model privacy" not in labels


def test_recovery_is_direct_for_private_users_and_group_members(
    mock_chat_model,
    mock_user_model,
) -> None:
    user = mock_user_model(
        inference_privacy_mode="private_only",
        inference_privacy_revision=1,
        free_inference_tos_version=None,
        free_inference_privacy_version=None,
        free_inference_accepted_at=None,
        free_inference_revoked_at=None,
    )
    private_chat = mock_chat_model(chat_type="private", telegram_id=42)
    private_text, private_markup = build_free_model_recovery(
        user,
        private_chat,
        context=InferenceContext.PRIVATE,
        can_manage=True,
    )
    group_text, group_markup = build_free_model_recovery(
        user,
        mock_chat_model(),
        context=InferenceContext.SUPERGROUP,
        can_manage=False,
    )

    assert "/buy" in private_text
    assert private_markup.inline_keyboard[0][0].text == "Enable free models"
    private_callback = InferencePrivacyCallback.unpack(
        private_markup.inline_keyboard[0][0].callback_data
    )
    assert private_callback.action is InferencePrivacyAction.REVIEW
    assert "/settings" in group_text
    assert "/buy" in group_text
    group_callback = ContextCallback.unpack(
        group_markup.inline_keyboard[0][0].callback_data
    )
    assert group_callback.action is ContextAction.MENU

    admin_text, admin_markup = build_free_model_recovery(
        user,
        mock_chat_model(),
        context=InferenceContext.SUPERGROUP,
        can_manage=True,
    )
    assert "/buy" in admin_text
    admin_callback = ChatFreeModelCallback.unpack(
        admin_markup.inline_keyboard[0][0].callback_data
    )
    assert admin_callback.action is ChatFreeModelAction.REVIEW


def test_ambient_cleanup_confirmation_names_scope_and_telegram_boundary() -> None:
    text, markup = build_ambient_cleanup_confirmation()

    assert "whole chat" in text
    assert "every topic" in text
    assert "from my memory" in text
    assert "Telegram messages stay" in text
    callback = ContextCallback.unpack(markup.inline_keyboard[0][0].callback_data)
    assert callback.action is ContextAction.CLEAN_AMBIENT
    assert callback.value == 0


@pytest.mark.asyncio
async def test_context_disable_first_renders_confirmation(
    make_message,
    make_user,
    mock_chat_model,
) -> None:
    message = make_message(text="settings")
    query = MagicMock(spec=CallbackQuery)
    query.message = message
    query.from_user = make_user(id=42)
    query.answer = AsyncMock()

    with patch(
        "derp.handlers.context_settings.actor_can_manage",
        new=AsyncMock(return_value=True),
    ):
        await confirm_disable_context(
            query,
            MagicMock(spec=Bot),
            mock_chat_model(ambient_history_enabled=True),
        )

    assert "Clean up my memory?" in message.edit_text.await_args.args[0]
    query.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_legacy_direct_off_button_cannot_bypass_confirmation(
    make_message,
    make_user,
    mock_chat_model,
) -> None:
    message = make_message(text="old settings")
    query = MagicMock(spec=CallbackQuery)
    query.message = message
    query.from_user = make_user(id=42)
    query.answer = AsyncMock()
    db = MagicMock()

    with patch(
        "derp.handlers.context_settings.actor_can_manage",
        new=AsyncMock(return_value=True),
    ):
        await toggle_context(
            query,
            ContextCallback(action=ContextAction.TOGGLE, value=0),
            db,
            MagicMock(spec=Bot),
            mock_chat_model(ambient_history_enabled=True),
        )

    db.session.assert_not_called()
    assert "Clean up my memory?" in message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_admin_acceptance_posts_visible_group_privacy_notice(
    make_message,
    make_user,
    mock_chat_model,
    mock_db_client,
    mock_user_model,
) -> None:
    message = make_message(text="review")
    query = MagicMock(spec=CallbackQuery)
    query.message = message
    query.from_user = make_user(id=42)
    query.answer = AsyncMock()
    chat = mock_chat_model(chat_id=UUID(int=2))
    user = mock_user_model(user_id=ADMIN_ID, telegram_id=42)
    policy = _enabled_policy()

    with (
        patch(
            "derp.handlers.context_settings.actor_can_manage",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "derp.handlers.context_settings.enable_chat_non_zdr_free_inference",
            new=AsyncMock(return_value=policy),
        ) as enable,
        patch(
            "derp.handlers.context_settings.ambient_delivery_available",
            new=AsyncMock(return_value=True),
        ),
    ):
        await accept_chat_free_model_terms(
            query,
            _callback(ChatFreeModelAction.ACCEPT),
            mock_db_client,
            MagicMock(spec=Bot),
            chat,
            user,
        )

    enable.assert_awaited_once()
    assert chat.free_inference_enabled is True
    assert "Free models are on" in message.answer.await_args.args[0]
    assert "/settings" in message.answer.await_args.args[0]
    assert "Free models: On" in message.edit_text.await_args.args[0]


@pytest.mark.asyncio
async def test_non_admin_cannot_accept_chat_policy(
    make_message,
    make_user,
    mock_chat_model,
    mock_db_client,
    mock_user_model,
) -> None:
    query = MagicMock(spec=CallbackQuery)
    query.message = make_message(text="review")
    query.from_user = make_user(id=42)
    query.answer = AsyncMock()

    with (
        patch(
            "derp.handlers.context_settings.actor_can_manage",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "derp.handlers.context_settings.enable_chat_non_zdr_free_inference",
            new=AsyncMock(),
        ) as enable,
    ):
        await accept_chat_free_model_terms(
            query,
            _callback(ChatFreeModelAction.ACCEPT),
            mock_db_client,
            MagicMock(spec=Bot),
            mock_chat_model(chat_id=UUID(int=2)),
            mock_user_model(user_id=ADMIN_ID, telegram_id=42),
        )

    enable.assert_not_awaited()
    query.answer.assert_awaited_once_with(
        "Only chat admins can change this",
        show_alert=True,
    )
