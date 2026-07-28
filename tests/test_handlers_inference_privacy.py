"""Inference privacy controls are explicit, private, and version-bound."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from aiogram.types import CallbackQuery
from babel.messages.pofile import read_po

from derp.db.inference_privacy import InferencePrivacyRevisionConflictError
from derp.handlers.context_settings import (
    ContextAction,
    ContextCallback,
    InferencePrivacyAction,
    InferencePrivacyCallback,
    accept_inference_privacy_terms,
    build_context_panel,
    build_inference_privacy_panel,
    build_inference_privacy_review,
    review_inference_privacy_terms,
    revoke_inference_privacy_terms,
    show_inference_privacy_menu,
)
from derp.inference import (
    FREE_INFERENCE_PRIVACY_URL,
    FREE_INFERENCE_PRIVACY_VERSION,
    FREE_INFERENCE_TOS_URL,
    FREE_INFERENCE_TOS_VERSION,
    InferencePrivacyMode,
    InferencePrivacyPreference,
)


def _buttons(markup):
    return [button for row in markup.inline_keyboard for button in row]


def _preference(
    *,
    tos_version: str = FREE_INFERENCE_TOS_VERSION,
    privacy_version: str = FREE_INFERENCE_PRIVACY_VERSION,
) -> InferencePrivacyPreference:
    return InferencePrivacyPreference(
        mode=InferencePrivacyMode.ALLOW_NON_ZDR_FREE,
        revision=2,
        accepted_tos_version=tos_version,
        accepted_privacy_version=privacy_version,
        accepted_at=datetime(2026, 7, 21, tzinfo=UTC),
    )


def _callback(
    action: InferencePrivacyAction,
    *,
    tos_version: str = FREE_INFERENCE_TOS_VERSION,
    privacy_version: str = FREE_INFERENCE_PRIVACY_VERSION,
    preference_revision: int = 1,
) -> InferencePrivacyCallback:
    return InferencePrivacyCallback(
        action=action,
        tos_version=tos_version,
        privacy_version=privacy_version,
        preference_revision=preference_revision,
    )


def _query(make_message, make_user, *, chat_type: str = "private"):
    chat_id = 42 if chat_type == "private" else -100
    query = MagicMock(spec=CallbackQuery)
    query.message = make_message(
        text="settings",
        user_id=42,
        chat_id=chat_id,
        chat_type=chat_type,
    )
    query.from_user = make_user(id=42)
    query.answer = AsyncMock()
    return query


def test_model_privacy_entry_is_private_chat_only(mock_chat_model) -> None:
    _, private_markup = build_context_panel(
        mock_chat_model(chat_type="private"),
        ambient_available=True,
        can_manage=True,
    )
    _, group_markup = build_context_panel(
        mock_chat_model(chat_type="supergroup"),
        ambient_available=True,
        can_manage=True,
    )

    private_buttons = _buttons(private_markup)
    entry = next(button for button in private_buttons if button.text == "Model privacy")
    assert entry.callback_data is not None
    assert ContextCallback.unpack(entry.callback_data).action is (
        ContextAction.INFERENCE_PRIVACY
    )
    assert "Model privacy" not in {button.text for button in _buttons(group_markup)}


def test_private_mode_offers_version_bound_review() -> None:
    text, markup = build_inference_privacy_panel(InferencePrivacyPreference())

    assert "Mode: Private" in text
    assert "zero-data-retention" in text
    assert "Groups stay private" in text
    action_button = markup.inline_keyboard[0][0]
    assert action_button.text == "Review free-model terms"
    assert action_button.callback_data is not None
    assert len(action_button.callback_data.encode()) <= 64
    callback = InferencePrivacyCallback.unpack(action_button.callback_data)
    assert callback == _callback(InferencePrivacyAction.REVIEW)


def test_current_acceptance_offers_one_action_revocation() -> None:
    text, markup = build_inference_privacy_panel(_preference())

    assert "Mode: Free models allowed" in text
    assert "Groups stay private" in text
    action_button = markup.inline_keyboard[0][0]
    assert action_button.text == "Use private models only"
    assert action_button.callback_data is not None
    callback = InferencePrivacyCallback.unpack(action_button.callback_data)
    assert callback.action is InferencePrivacyAction.REVOKE
    assert callback.preference_revision == 2


def test_old_legal_acceptance_is_effectively_private() -> None:
    text, markup = build_inference_privacy_panel(
        _preference(
            tos_version="openrouter-tos-2026-06-01",
            privacy_version="openrouter-privacy-2026-06-01",
        )
    )

    assert "Mode: Private" in text
    assert "terms changed" in text
    action_button = markup.inline_keyboard[0][0]
    assert action_button.callback_data is not None
    callback = InferencePrivacyCallback.unpack(action_button.callback_data)
    assert callback.action is InferencePrivacyAction.REVIEW
    assert callback.preference_revision == 2


def test_review_links_and_acceptance_match_current_legal_versions() -> None:
    text, markup = build_inference_privacy_review(7)

    assert "may store prompts and replies" in text
    assert "Groups stay private" in text
    buttons = _buttons(markup)
    assert {button.text: button.url for button in buttons if button.url} == {
        "Terms": FREE_INFERENCE_TOS_URL,
        "Privacy": FREE_INFERENCE_PRIVACY_URL,
    }
    accept = next(button for button in buttons if button.text.startswith("I agree"))
    assert accept.callback_data is not None
    assert len(accept.callback_data.encode()) <= 64
    assert InferencePrivacyCallback.unpack(accept.callback_data) == _callback(
        InferencePrivacyAction.ACCEPT,
        preference_revision=7,
    )


def test_callback_remains_within_telegram_limit_at_max_database_revision() -> None:
    callback_data = _callback(
        InferencePrivacyAction.ACCEPT,
        preference_revision=2_147_483_647,
    ).pack()

    assert len(callback_data.encode()) <= 64


@pytest.mark.asyncio
async def test_private_menu_loads_only_the_callback_actor_preference(
    make_message,
    make_user,
    mock_db_client,
    mock_user_model,
) -> None:
    query = _query(make_message, make_user)
    user = mock_user_model(user_id=UUID(int=1), telegram_id=42)
    preference = InferencePrivacyPreference()
    session = mock_db_client.read_session.return_value.__aenter__.return_value

    with patch(
        "derp.handlers.context_settings.get_inference_privacy_preference",
        new=AsyncMock(return_value=preference),
    ) as load_preference:
        await show_inference_privacy_menu(query, mock_db_client, user)

    load_preference.assert_awaited_once_with(session, user.id)
    text, markup = build_inference_privacy_panel(preference)
    query.message.edit_text.assert_awaited_once_with(text, reply_markup=markup)
    query.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_forged_group_acceptance_cannot_change_preference(
    make_message,
    make_user,
    mock_db_client,
    mock_user_model,
) -> None:
    query = _query(make_message, make_user, chat_type="supergroup")
    user = mock_user_model(user_id=UUID(int=1), telegram_id=42)

    with patch(
        "derp.handlers.context_settings.accept_non_zdr_free_inference",
        new_callable=AsyncMock,
    ) as accept:
        await accept_inference_privacy_terms(
            query,
            _callback(InferencePrivacyAction.ACCEPT),
            mock_db_client,
            user,
        )

    accept.assert_not_awaited()
    mock_db_client.session.assert_not_called()
    query.message.edit_text.assert_not_awaited()
    query.answer.assert_awaited_once_with(
        "Open model privacy in your private chat.", show_alert=True
    )


@pytest.mark.asyncio
async def test_stale_acceptance_requires_review_without_database_write(
    make_message,
    make_user,
    mock_db_client,
    mock_user_model,
) -> None:
    query = _query(make_message, make_user)
    user = mock_user_model(user_id=UUID(int=1), telegram_id=42)
    preference = InferencePrivacyPreference()

    with (
        patch(
            "derp.handlers.context_settings.accept_non_zdr_free_inference",
            new_callable=AsyncMock,
        ) as accept,
        patch(
            "derp.handlers.context_settings.get_inference_privacy_preference",
            new=AsyncMock(return_value=preference),
        ),
    ):
        await accept_inference_privacy_terms(
            query,
            _callback(
                InferencePrivacyAction.ACCEPT,
                tos_version="openrouter-tos-2026-06-01",
            ),
            mock_db_client,
            user,
        )

    accept.assert_not_awaited()
    mock_db_client.session.assert_not_called()
    text, markup = build_inference_privacy_review(preference.revision)
    query.message.edit_text.assert_awaited_once_with(text, reply_markup=markup)
    query.answer.assert_awaited_once_with(
        "The terms changed. Review the latest versions.", show_alert=True
    )


@pytest.mark.asyncio
async def test_acceptance_persists_exact_reviewed_versions(
    make_message,
    make_user,
    mock_db_client,
    mock_user_model,
) -> None:
    query = _query(make_message, make_user)
    user = mock_user_model(user_id=UUID(int=1), telegram_id=42)
    preference = _preference()
    session = mock_db_client.session.return_value.__aenter__.return_value

    with patch(
        "derp.handlers.context_settings.accept_non_zdr_free_inference",
        new=AsyncMock(return_value=preference),
    ) as accept:
        await accept_inference_privacy_terms(
            query,
            _callback(InferencePrivacyAction.ACCEPT),
            mock_db_client,
            user,
        )

    accept.assert_awaited_once_with(
        session,
        user.id,
        expected_revision=1,
        tos_version=FREE_INFERENCE_TOS_VERSION,
        privacy_version=FREE_INFERENCE_PRIVACY_VERSION,
    )
    assert "Mode: Free models allowed" in query.message.edit_text.await_args.args[0]
    query.answer.assert_awaited_once_with(
        "Free models are now allowed in private chat and inline mode."
    )


@pytest.mark.asyncio
async def test_revocation_returns_to_private_mode_in_one_action(
    make_message,
    make_user,
    mock_db_client,
    mock_user_model,
) -> None:
    query = _query(make_message, make_user)
    user = mock_user_model(user_id=UUID(int=1), telegram_id=42)
    preference = InferencePrivacyPreference()
    session = mock_db_client.session.return_value.__aenter__.return_value

    with patch(
        "derp.handlers.context_settings.revoke_non_zdr_free_inference",
        new=AsyncMock(return_value=preference),
    ) as revoke:
        await revoke_inference_privacy_terms(
            query,
            _callback(InferencePrivacyAction.REVOKE, preference_revision=2),
            mock_db_client,
            user,
        )

    revoke.assert_awaited_once_with(session, user.id, expected_revision=2)
    assert "Mode: Private" in query.message.edit_text.await_args.args[0]
    query.answer.assert_awaited_once_with("Private models only.")


@pytest.mark.asyncio
async def test_replayed_review_is_rejected_before_rendering_acceptance(
    make_message,
    make_user,
    mock_db_client,
    mock_user_model,
) -> None:
    query = _query(make_message, make_user)
    user = mock_user_model(user_id=UUID(int=1), telegram_id=42)
    current = _preference()

    with patch(
        "derp.handlers.context_settings.get_inference_privacy_preference",
        new=AsyncMock(return_value=current),
    ):
        await review_inference_privacy_terms(
            query,
            _callback(InferencePrivacyAction.REVIEW, preference_revision=1),
            mock_db_client,
            user,
        )

    assert "Mode: Free models allowed" in query.message.edit_text.await_args.args[0]
    query.answer.assert_awaited_once_with(
        "This model privacy button expired. Open the setting again.",
        show_alert=True,
    )


@pytest.mark.asyncio
async def test_replayed_acceptance_cannot_reenable_after_revocation(
    make_message,
    make_user,
    mock_db_client,
    mock_user_model,
) -> None:
    query = _query(make_message, make_user)
    user = mock_user_model(user_id=UUID(int=1), telegram_id=42)
    current = _preference().revoke_non_zdr_free(
        revoked_at=datetime(2026, 7, 21, 1, tzinfo=UTC)
    )
    conflict = InferencePrivacyRevisionConflictError(
        expected_revision=1,
        current=current,
    )

    with patch(
        "derp.handlers.context_settings.accept_non_zdr_free_inference",
        new=AsyncMock(side_effect=conflict),
    ) as accept:
        await accept_inference_privacy_terms(
            query,
            _callback(InferencePrivacyAction.ACCEPT, preference_revision=1),
            mock_db_client,
            user,
        )

    accept.assert_awaited_once()
    assert "Mode: Private" in query.message.edit_text.await_args.args[0]
    query.answer.assert_awaited_once_with(
        "This model privacy button expired. Open the setting again.",
        show_alert=True,
    )


def test_russian_source_copy_is_complete() -> None:
    with Path("derp/locales/ru/LC_MESSAGES/messages.po").open(encoding="utf-8") as file:
        catalog = read_po(file, locale="ru")

    expected = {
        "Model privacy": "Приватность моделей",
        "Mode: Private": "Режим: приватный",
        "Mode: Free models allowed": "Режим: бесплатные модели разрешены",
        "Review free-model terms": "Условия бесплатных моделей",
        "Use private models only": "Только приватные модели",
        "I agree, allow free models": "Принимаю и разрешаю",
        "Private models only.": "Только приватные модели.",
    }
    assert {message: catalog.get(message).string for message in expected} == expected
