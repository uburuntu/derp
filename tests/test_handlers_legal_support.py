"""Private legal acceptance and content-free support adapters."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from aiogram import Bot
from aiogram.enums import MessageEntityType
from aiogram.types import CallbackQuery

from derp.common.legal_documents import LegalDocumentKind, legal_document_pages
from derp.handlers.legal_support import (
    SUPPORT_MENU_CALLBACK,
    LegalCloseCallback,
    LegalDocumentCallback,
    SupportCreateCallback,
    SupportMenuCallback,
    SupportReplyFilter,
    TermsAcceptanceSource,
    TermsAcceptCallback,
    accept_terms,
    build_legal_document_page,
    build_support_panel,
    build_terms_panel,
    create_support_case,
    navigate_support,
    show_legal_document,
    show_support,
)
from derp.history.capture import capture_outbound_history, should_capture_outbound
from derp.legal import TERMS_ACCEPTANCE_VERSION
from derp.operator import OperatorControlConfig
from derp.support import (
    SupportCase,
    SupportIntakeLookup,
    SupportIntakeLookupState,
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
    query.from_user.language_code = "en"
    query.answer = AsyncMock()
    return query


@pytest.mark.asyncio
async def test_support_reply_filter_uses_durable_prompt_identity(
    make_message,
) -> None:
    prompt = make_message(
        text="Copy from an older deployment",
        user_id=99,
        chat_id=42,
        chat_type="private",
        message_id=8,
    )
    message = make_message(
        text="The charge is missing",
        user_id=42,
        chat_id=42,
        chat_type="private",
        message_id=9,
        reply_to_message=prompt,
    )
    lookup = SupportIntakeLookup(SupportIntakeLookupState.EXPIRED)
    service = MagicMock()
    service.lookup_intake_for_telegram_user = AsyncMock(return_value=lookup)

    matched = await SupportReplyFilter()(message, service)

    assert matched == {"support_intake_lookup": lookup}
    service.lookup_intake_for_telegram_user.assert_awaited_once_with(
        42,
        prompt_chat_id=42,
        prompt_message_id=8,
    )


def test_panels_expose_in_bot_legal_pages() -> None:
    terms_text, terms_markup = build_terms_panel(
        accepted=False,
        actor_telegram_id=42,
    )
    support_text, support_markup = build_support_panel((_case(),))

    assert "Accept these terms" in terms_text
    legal_callbacks = [
        LegalDocumentCallback.unpack(button.callback_data)
        for row in terms_markup.inline_keyboard
        for button in row
        if button.callback_data and button.callback_data.startswith("legal:")
    ]
    assert {callback.document for callback in legal_callbacks} == {
        LegalDocumentKind.TERMS,
        LegalDocumentKind.PRIVACY,
    }
    assert all(callback.actor_id == 42 for callback in legal_callbacks)
    assert all(callback.separate for callback in legal_callbacks)
    assert not any(button.url for row in terms_markup.inline_keyboard for button in row)
    assert "A1B2C3D4E5" in support_text
    assert all(
        button.callback_data != "force_reply"
        for row in support_markup.inline_keyboard
        for button in row
    )


@pytest.mark.asyncio
async def test_terms_acceptance_is_actor_and_version_bound_and_persisted(
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
            actor_id=42,
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


def test_legal_reader_uses_collapsed_bounded_pages() -> None:
    pages = legal_document_pages(LegalDocumentKind.PRIVACY, "ru")
    content, markup = build_legal_document_page(pages[0], actor_telegram_id=42)

    text, entities = content.render()

    assert len(text) < 4096
    assert len(pages) > 1
    assert any(
        entity.type == MessageEntityType.EXPANDABLE_BLOCKQUOTE for entity in entities
    )
    next_page = LegalDocumentCallback.unpack(markup.inline_keyboard[0][0].callback_data)
    assert next_page.page == 1
    assert next_page.actor_id == 42
    close = LegalCloseCallback.unpack(markup.inline_keyboard[-1][0].callback_data)
    assert close.actor_id == 42


@pytest.mark.asyncio
async def test_opening_legal_page_keeps_purchase_gate_message_intact(
    make_message,
    mock_user_model,
) -> None:
    message = make_message(user_id=42, chat_type="supergroup")
    query = _callback(message)

    await show_legal_document(
        query,
        LegalDocumentCallback(
            document=LegalDocumentKind.TERMS,
            actor_id=42,
            separate=True,
        ),
        mock_user_model(telegram_id=42, language_code="en"),
    )

    message.edit_text.assert_not_awaited()
    message.answer.assert_awaited_once()
    assert message.answer.await_args.kwargs["protect_content"] is True
    query.answer.assert_awaited_once_with()


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
    assert "stay private" in message.reply.await_args.args[0]
    button = message.reply.await_args.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "Open support"
    assert button.url.endswith("?start=support")


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

    callback_data = SupportMenuCallback.unpack(SUPPORT_MENU_CALLBACK)
    assert callback_data.source is SupportSource.PRIVACY
    await navigate_support(query, callback_data, service, user)

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
async def test_refund_category_asks_for_an_exact_payment(
    make_message,
    mock_user_model,
) -> None:
    message = make_message(user_id=42, chat_id=42, chat_type="private")
    query = _callback(message)
    service = MagicMock()
    service.list_payments = AsyncMock(return_value=())
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

    service.list_payments.assert_awaited_once_with(user.id)
    assert "No payments found" in message.edit_text.await_args.args[0]
    service.open.assert_not_called()
    bot.send_message.assert_not_awaited()
    query.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_nonpayment_category_creates_a_durable_reply_prompt(
    make_message,
    mock_user_model,
) -> None:
    message = make_message(user_id=42, chat_id=42, chat_type="private")
    query = _callback(message)
    service = MagicMock()
    service.register_intake = AsyncMock()
    capture_states: list[bool] = []

    async def rendered_prompt(*args, **kwargs):
        capture_states.append(should_capture_outbound())
        return message

    bot = MagicMock(spec=Bot)
    message.answer = AsyncMock(side_effect=rendered_prompt)
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

    service.register_intake.assert_awaited_once()
    assert service.register_intake.await_args.kwargs["kind"] is SupportKind.ACCESS
    assert message.answer.await_args.args[0] == "What happened? One message is enough."
    query.answer.assert_awaited_once_with()
    assert capture_states == [False]
