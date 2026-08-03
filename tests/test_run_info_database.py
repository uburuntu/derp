"""PostgreSQL projection tests for reply-based run receipts."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from derp.inference_usage import (
    InferenceOutcome,
    InferenceTokenUsage,
    InferenceUsageCompletion,
    InferenceUsageId,
    InferenceUsageRepository,
    InferenceUsageStart,
)
from derp.models import OperationQuote, PaidOperation
from derp.run_info import ChatRunDelivery, RunInfoService, RunPrivacyMode

pytestmark = pytest.mark.database
NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)


async def test_delivered_chat_run_joins_usage_context_and_captured_charge(
    db_session: AsyncSession,
    user_factory,
    chat_factory,
) -> None:
    user = await user_factory(telegram_id=71_001)
    chat = await chat_factory(telegram_id=-71_001)
    operation_id = uuid4()
    quote = OperationQuote(
        operation_id=operation_id,
        request_key=f"run-info:{operation_id}",
        requester_id=user.id,
        chat_id=chat.id,
        feature="chat",
        model_key="chat_standard",
        provider="openrouter",
        provider_model_id="anthropic/claude-sonnet-5",
        context_band="small",
        variant="default",
        amount_credits=116,
        estimated_provider_cost_usd=Decimal("0.01"),
        pricing_version="test-v1",
        catalog_verified_on=date(2026, 8, 3),
        pricing_input={"input_tokens": 1200},
        expires_at=NOW + timedelta(minutes=10),
        created_at=NOW,
    )
    db_session.add(quote)
    await db_session.flush()
    db_session.add(
        PaidOperation(
            id=operation_id,
            quote_id=quote.id,
            state="captured",
            captured_at=NOW,
        )
    )
    await db_session.flush()

    @asynccontextmanager
    async def transactions():
        yield db_session

    usage = InferenceUsageRepository(transactions)
    attempt = InferenceUsageStart(
        id=InferenceUsageId.new(),
        operation_id=operation_id,
        user_id=user.id,
        chat_id=chat.id,
        provider="openrouter",
        provider_model_id="anthropic/claude-sonnet-5",
        started_at=NOW,
    )
    await usage.register(attempt)
    await usage.complete(
        attempt.id,
        InferenceUsageCompletion(
            outcome=InferenceOutcome.SUCCEEDED,
            completed_at=NOW + timedelta(seconds=1),
            tokens=InferenceTokenUsage(
                input_tokens=1000,
                output_tokens=200,
                total_tokens=1200,
                cache_read_tokens=100,
                reasoning_tokens=25,
            ),
        ),
    )
    service = RunInfoService(transactions)
    await service.record_chat_delivery(
        ChatRunDelivery(
            operation_id=operation_id,
            chat_id=chat.id,
            requester_id=user.id,
            request_message_id=10,
            response_message_ids=(11, 12),
            model_key="chat_standard",
            model_display_name="Claude Sonnet 5",
            privacy_mode=RunPrivacyMode.PRIVATE,
            context_messages=8,
            context_turns=4,
            context_estimated_tokens=900,
        )
    )

    info = await service.get_for_telegram_message(
        telegram_chat_id=chat.telegram_id,
        telegram_message_id=11,
        viewer_telegram_id=user.telegram_id,
    )

    assert info is not None
    assert info.model_display_name == "Claude Sonnet 5"
    assert info.tokens is not None and info.tokens.total_tokens == 1200
    assert info.context_messages == 8
    assert info.context_estimated_tokens == 900
    assert info.charged_credits == 116
    assert info.charge_visible is True

    other_user = await user_factory(telegram_id=71_002)
    shared = await service.get_for_telegram_message(
        telegram_chat_id=chat.telegram_id,
        telegram_message_id=11,
        viewer_telegram_id=other_user.telegram_id,
    )
    assert shared is not None
    assert shared.charge_visible is False
