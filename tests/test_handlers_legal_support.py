"""Private legal acceptance and content-free support adapters."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from aiogram import Bot
from aiogram.types import CallbackQuery

from derp.handlers.legal_support import (
    SUPPORT_MENU_CALLBACK,
    SupportCreateCallback,
    TermsAcceptanceSource,
    TermsAcceptCallback,
    accept_terms,
    build_support_panel,
    build_terms_panel,
    create_support_case,
    navigate_support,
    show_support,
)
from derp.history.capture import capture_outbound_history, should_capture_outbound
from derp.legal import TERMS_ACCEPTANCE_VERSION
from derp.operator import OperatorControlConfig
from derp.support import (
    OpenSupportResult,
    SupportCase,
    SupportKind,
    SupportSource,
    SupportStatus,
)

NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)


def _config() -> OperatorControlConfig:
    return OperatorControlConfig(
        environment="prod",
        service_version="0.1.0",
        public_purchases_enabled=True,
        ai_content_capture_enabled=False,
        operator_ids={7},
    )


def _case(kind: SupportKind = SupportKind.PAYMENT) -> SupportCase:
    return SupportCase(
        reference="A1B2C3D4E5",
        kind=kind,
        status=SupportStatus.OPEN,
        created_at=NOW,
    )


def _callback(message, *, user_id: int = 42) -> CallbackQuery:
    query = MagicMock(spec=CallbackQuery)
    query.message = message
    query.from_user = MagicMock()
    query.from_user.id = user_id
    query.answer = AsyncMock()
    return query


def test_panels_expose_immutable_legal_links_and_no_free_form_input() -> None:
    terms_text, terms_markup = build_terms_panel(accepted=False)
    support_text, support_markup = build_support_panel((_case(),))

    assert "Accept these terms" in terms_text
    assert any(button.url for row in terms_markup.inline_keyboard for button in row)
    assert "A1B2C3D4E5" in support_text
    assert "free-form support message" in support_text
    assert all(
        button.callback_data != "force_reply"
        for row in support_markup.inline_keyboard
        for button in row
    )


@pytest.mark.asyncio
async def test_terms_acceptance_is_private_version_bound_and_persisted(
    make_message,
    mock_user_model,
) -> None:
    message = make_message(
        text="terms",
        user_id=42,
        chat_id=42,
        chat_type="private",
    )
    query = _callback(message)
    acceptance = MagicMock()
    acceptance.accept_current = AsyncMock()
    user = mock_user_model(user_id=UUID(int=1), telegram_id=42)

    await accept_terms(
        query,
        TermsAcceptCallback(
            version=TERMS_ACCEPTANCE_VERSION,
            source=TermsAcceptanceSource.PURCHASE_GATE,
        ),
        acceptance,
        user,
    )

    acceptance.accept_current.assert_awaited_once_with(
        user.id,
        source="purchase_gate",
    )
    assert "Accepted for purchases" in message.edit_text.await_args.args[0]
    query.answer.assert_awaited_once_with("Terms accepted")


@pytest.mark.asyncio
async def test_group_support_redirects_without_creating_case(
    make_message,
    mock_user_model,
) -> None:
    message = make_message(text="/support", chat_type="supergroup")
    service = MagicMock()
    service.list_open = AsyncMock()

    await show_support(message, service, mock_user_model())

    service.list_open.assert_not_awaited()
    assert "/support" in message.reply.await_args.args[0]


@pytest.mark.asyncio
async def test_private_support_buttons_retain_support_provenance(
    make_message,
    mock_user_model,
) -> None:
    message = make_message(user_id=42, chat_id=42, chat_type="private")
    service = MagicMock()
    service.list_open = AsyncMock(return_value=())

    await show_support(message, service, mock_user_model())

    markup = message.answer.await_args.kwargs["reply_markup"]
    callbacks = [
        SupportCreateCallback.unpack(button.callback_data)
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("support:")
    ]
    assert callbacks
    assert all(item.source is SupportSource.SUPPORT for item in callbacks)


@pytest.mark.asyncio
async def test_privacy_contact_opens_support_without_creating_case(
    make_message,
    mock_user_model,
) -> None:
    message = make_message(user_id=42, chat_id=42, chat_type="private")
    query = _callback(message)
    service = MagicMock()
    service.list_open = AsyncMock(return_value=())
    user = mock_user_model(user_id=UUID(int=1), telegram_id=42)

    assert SUPPORT_MENU_CALLBACK == "support:menu"
    await navigate_support(query, service, user)

    service.list_open.assert_awaited_once_with(user.id)
    assert "Support" in message.edit_text.await_args.args[0]
    callbacks = [
        SupportCreateCallback.unpack(button.callback_data)
        for row in message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("support:")
    ]
    assert callbacks
    assert all(item.source is SupportSource.PRIVACY for item in callbacks)
    query.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_new_case_is_deduplicated_by_service_and_notifies_operator(
    make_message,
    mock_user_model,
) -> None:
    message = make_message(user_id=42, chat_id=42, chat_type="private")
    query = _callback(message)
    case = _case(SupportKind.REFUND)
    service = MagicMock()
    service.open = AsyncMock(return_value=OpenSupportResult(case, created=True))
    service.list_open = AsyncMock(return_value=(case,))
    bot = MagicMock(spec=Bot)
    bot.send_message = AsyncMock()
    user = mock_user_model(user_id=UUID(int=1), telegram_id=42)

    await create_support_case(
        query,
        SupportCreateCallback(
            kind=SupportKind.REFUND,
            source=SupportSource.PRIVACY,
        ),
        service,
        bot,
        _config(),
        user,
    )

    service.open.assert_awaited_once_with(
        user.id,
        kind=SupportKind.REFUND,
        source=SupportSource.PRIVACY,
    )
    callbacks = [
        SupportCreateCallback.unpack(button.callback_data)
        for row in message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("support:")
    ]
    assert callbacks
    assert all(item.source is SupportSource.PRIVACY for item in callbacks)
    notice = bot.send_message.await_args.args[1]
    assert "A1B2C3D4E5" in notice
    assert "42" not in notice
    query.answer.assert_awaited_once_with("Case opened")


@pytest.mark.asyncio
async def test_operator_alert_failure_does_not_break_durable_support_panel(
    make_message,
    mock_user_model,
) -> None:
    message = make_message(user_id=42, chat_id=42, chat_type="private")
    query = _callback(message)
    case = _case(SupportKind.ACCESS)
    service = MagicMock()
    service.open = AsyncMock(return_value=OpenSupportResult(case, created=True))
    service.list_open = AsyncMock(return_value=(case,))
    capture_states: list[bool] = []

    async def failed_alert(*args, **kwargs) -> None:
        capture_states.append(should_capture_outbound())
        raise RuntimeError("operator delivery failed")

    async def rendered_panel(*args, **kwargs) -> None:
        capture_states.append(should_capture_outbound())

    bot = MagicMock(spec=Bot)
    bot.send_message = AsyncMock(side_effect=failed_alert)
    message.edit_text = AsyncMock(side_effect=rendered_panel)
    user = mock_user_model(user_id=UUID(int=1), telegram_id=42)

    with capture_outbound_history():
        await create_support_case(
            query,
            SupportCreateCallback(
                kind=SupportKind.ACCESS,
                source=SupportSource.SUPPORT,
            ),
            service,
            bot,
            _config(),
            user,
        )
        assert should_capture_outbound()

    service.list_open.assert_awaited_once_with(user.id)
    message.edit_text.assert_awaited_once()
    query.answer.assert_awaited_once_with("Case opened")
    assert capture_states == [False, False]
