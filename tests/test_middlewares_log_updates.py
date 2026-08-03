"""Tests for privacy-safe Telegram update tracing."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import logfire
import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Chat, Message, PreCheckoutQuery, Update, User
from logfire.testing import CaptureLogfire

from derp.common.update_context import UpdateContext, update_ctx
from derp.middlewares.log_updates import LogUpdatesMiddleware


@pytest.fixture
def middleware() -> LogUpdatesMiddleware:
    return LogUpdatesMiddleware()


@pytest.fixture
def make_update():
    def _make_update(
        text: str = "private-message-sentinel",
        user_id: int = 12_345,
        chat_id: int = -100_123,
        thread_id: int | None = 77,
    ) -> Update:
        user = User(
            id=user_id,
            is_bot=False,
            first_name="Private Name",
            username="private_username_sentinel",
        )
        chat = Chat(
            id=chat_id,
            type="supergroup",
            title="private-chat-title-sentinel",
        )
        message = Message(
            message_id=42,
            date=datetime.now(UTC),
            chat=chat,
            from_user=user,
            text=text,
            is_topic_message=thread_id is not None,
            message_thread_id=thread_id,
        )
        return Update(update_id=9, message=message)

    return _make_update


def _application_attributes(span: dict) -> dict:
    return {
        key: value
        for key, value in span["attributes"].items()
        if key.startswith(("telegram.", "derp."))
    }


class TestLogUpdatesMiddleware:
    @pytest.mark.asyncio
    async def test_emits_safe_root_span_and_scopes_context(
        self,
        middleware: LogUpdatesMiddleware,
        make_update,
        capfire: CaptureLogfire,
    ) -> None:
        capfire.exporter.clear()
        update = make_update()

        async def handler(event: Update, data: dict) -> object:
            assert event is update
            assert data == {}
            assert update_ctx.get() == UpdateContext(
                update_id=9,
                chat_id=-100_123,
                user_id=12_345,
                thread_id=77,
            )
            with logfire.span("handler.work"):
                return object()

        result = await middleware(handler, update, {})

        assert result is not UNHANDLED
        assert update_ctx.get() is None

        spans = capfire.exporter.exported_spans_as_dict()
        root = next(span for span in spans if span["name"] == "telegram.update")
        child = next(span for span in spans if span["name"] == "handler.work")
        assert child["parent"] == root["context"]
        assert _application_attributes(root) == {
            "telegram.update_id": 9,
            "telegram.update_type": "message",
            "telegram.chat_id": -100_123,
            "telegram.user_id": 12_345,
            "telegram.message_id": 42,
            "telegram.thread_id": 77,
            "derp.update_handled": True,
        }

        serialized = json.dumps(spans)
        for private_value in (
            "private-message-sentinel",
            "private_username_sentinel",
            "private-chat-title-sentinel",
            "Private Name",
        ):
            assert private_value not in serialized

    @pytest.mark.asyncio
    async def test_marks_unhandled_update(
        self,
        middleware: LogUpdatesMiddleware,
        make_update,
        capfire: CaptureLogfire,
    ) -> None:
        capfire.exporter.clear()

        result = await middleware(AsyncMock(return_value=UNHANDLED), make_update(), {})

        assert result is UNHANDLED
        root = next(
            span
            for span in capfire.exporter.exported_spans_as_dict()
            if span["name"] == "telegram.update"
        )
        assert root["attributes"]["derp.update_handled"] is False

    @pytest.mark.asyncio
    async def test_records_exception_and_restores_previous_context(
        self,
        middleware: LogUpdatesMiddleware,
        make_update,
        capfire: CaptureLogfire,
    ) -> None:
        capfire.exporter.clear()
        previous = UpdateContext(update_id=1, chat_id=2, user_id=3, thread_id=None)
        token = update_ctx.set(previous)

        async def failing_handler(event: Update, data: dict) -> None:
            raise ValueError("handler exploded")

        try:
            with pytest.raises(ValueError, match="handler exploded"):
                await middleware(failing_handler, make_update(), {})
            assert update_ctx.get() == previous
        finally:
            update_ctx.reset(token)

        spans = capfire.exporter.exported_spans_as_dict()
        root = next(span for span in spans if span["name"] == "telegram.update")
        exception_events = [
            event for event in root.get("events", []) if event["name"] == "exception"
        ]
        assert len(exception_events) == 1
        assert exception_events[0]["attributes"]["exception.type"] == "ValueError"
        assert exception_events[0]["attributes"]["exception.escaped"] == "True"
        assert exception_events[0]["attributes"]["exception.message"] == (
            "Exception details redacted"
        )
        assert "handler exploded" not in json.dumps(root)
        assert root["attributes"]["error.type"] == "ValueError"
        assert root["attributes"]["logfire.level_num"] == 17

    @pytest.mark.asyncio
    async def test_cancellation_is_not_recorded_as_failure(
        self,
        middleware: LogUpdatesMiddleware,
        make_update,
        capfire: CaptureLogfire,
    ) -> None:
        capfire.exporter.clear()

        async def cancelled_handler(event: Update, data: dict) -> None:
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await middleware(cancelled_handler, make_update(), {})

        root = next(
            span
            for span in capfire.exporter.exported_spans_as_dict()
            if span["name"] == "telegram.update"
        )
        assert root.get("events", []) == []
        assert root["attributes"]["derp.update_cancelled"] is True
        assert "logfire.level_num" not in root["attributes"]
        assert update_ctx.get() is None

    @pytest.mark.asyncio
    async def test_does_not_capture_payment_payload(
        self,
        middleware: LogUpdatesMiddleware,
        capfire: CaptureLogfire,
    ) -> None:
        capfire.exporter.clear()
        user = User(id=55, is_bot=False, first_name="Payer")
        update = Update(
            update_id=10,
            pre_checkout_query=PreCheckoutQuery(
                id="private-query-id-sentinel",
                from_user=user,
                currency="XTR",
                total_amount=100,
                invoice_payload="private-invoice-payload-sentinel",
            ),
        )

        await middleware(AsyncMock(return_value=MagicMock()), update, {})

        spans = capfire.exporter.exported_spans_as_dict()
        serialized = json.dumps(spans)
        assert "private-query-id-sentinel" not in serialized
        assert "private-invoice-payload-sentinel" not in serialized
        root = next(span for span in spans if span["name"] == "telegram.update")
        assert _application_attributes(root) == {
            "telegram.update_id": 10,
            "telegram.update_type": "pre_checkout_query",
            "telegram.user_id": 55,
            "derp.update_handled": True,
        }

    @pytest.mark.asyncio
    async def test_rejects_non_update_event(
        self,
        middleware: LogUpdatesMiddleware,
    ) -> None:
        with pytest.raises(RuntimeError, match="unexpected event type"):
            await middleware(AsyncMock(), MagicMock(), {})
