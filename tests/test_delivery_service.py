"""Durable delivery never guesses whether Telegram accepted a result."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from derp.artifacts import FilesystemArtifactStore
from derp.delivery import (
    Delivered,
    DeliveryFailed,
    DeliveryService,
    DeliveryStateError,
    DeliveryTarget,
    DeliveryUncertain,
)
from derp.features import MediaContent
from derp.media import MediaFamily
from derp.models import Chat, DeliveryIntent, OperationQuote, PaidOperation, User
from derp.operations import OperationId

pytestmark = pytest.mark.database


@dataclass(slots=True)
class MutableClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@dataclass(frozen=True, slots=True)
class DeliveryEnvironment:
    transactions: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    service: DeliveryService
    bot: MagicMock
    reversal: MagicMock
    clock: MutableClock


@pytest_asyncio.fixture
async def delivery_env(
    db_engine: AsyncEngine, tmp_path
) -> AsyncIterator[DeliveryEnvironment]:
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    @asynccontextmanager
    async def transactions() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session, session.begin():
            yield session

    bot = MagicMock(spec=Bot)
    bot.send_photo = AsyncMock()
    bot.send_media_group = AsyncMock()
    reversal = MagicMock()
    reversal.reverse = AsyncMock()
    clock = MutableClock(datetime.now(UTC))
    service = DeliveryService(
        transactions,
        FilesystemArtifactStore(tmp_path / "artifacts"),
        bot,
        reversal,
        clock=clock,
        artifact_ttl=timedelta(hours=1),
    )
    yield DeliveryEnvironment(transactions, service, bot, reversal, clock)


async def _operation(env: DeliveryEnvironment) -> OperationId:
    operation_id = OperationId(uuid4())
    async with env.transactions() as session:
        user = User(
            telegram_id=10_000_000 + uuid4().int % 9_000_000_000,
            is_bot=False,
            first_name="Delivery",
        )
        chat = Chat(
            telegram_id=-(10_000_000 + uuid4().int % 9_000_000_000),
            type="supergroup",
        )
        session.add_all([user, chat])
        await session.flush()
        quote = OperationQuote(
            operation_id=operation_id.value,
            request_key=f"delivery-service:{operation_id}",
            requester_id=user.id,
            chat_id=chat.id,
            feature="image_generate",
            model_key="image",
            provider_model_id="gemini-test",
            context_band="small",
            variant="resolution=1K",
            amount_credits=10,
            estimated_provider_cost_usd=Decimal("0.01"),
            pricing_version="test-v1",
            catalog_verified_on=date(2026, 7, 20),
            pricing_input={"resolution": "1K"},
            expires_at=env.clock() + timedelta(minutes=10),
        )
        session.add(quote)
        await session.flush()
        session.add(
            PaidOperation(
                id=operation_id.value,
                quote_id=quote.id,
                state="executing",
            )
        )
    return operation_id


async def _prepare_and_capture(
    env: DeliveryEnvironment,
    operation_id: OperationId,
    *,
    media_count: int = 1,
) -> None:
    media = tuple(
        MediaContent(MediaFamily.IMAGE, "image/png", f"image-{index}".encode())
        for index in range(media_count)
    )
    prepared = await env.service.persist_result(
        operation_id,
        media=media,
        target=DeliveryTarget(-1001, 77, 42),
        caption="Result",
    )
    duplicate = await env.service.persist_result(
        operation_id,
        media=media,
        target=DeliveryTarget(-1001, 77, 42),
        caption="Result",
    )
    assert prepared.artifact_count == media_count
    assert duplicate.idempotent
    assert duplicate.intent_id == prepared.intent_id
    async with env.transactions() as session:
        operation = await session.get(PaidOperation, operation_id.value)
        assert operation is not None
        operation.state = "captured"
        operation.captured_at = env.clock()
    await env.service.mark_ready(operation_id)


def _message(message_id: int) -> MagicMock:
    message = MagicMock(spec=Message)
    message.message_id = message_id
    return message


async def test_delivery_acknowledgement_is_persisted_and_retry_is_idempotent(
    delivery_env: DeliveryEnvironment,
) -> None:
    operation_id = await _operation(delivery_env)
    await _prepare_and_capture(delivery_env, operation_id)
    delivery_env.bot.send_photo.return_value = _message(101)

    first = await delivery_env.service.deliver(operation_id)
    duplicate = await delivery_env.service.deliver(operation_id)

    assert first == duplicate == Delivered((101,))
    delivery_env.bot.send_photo.assert_awaited_once()
    delivery_env.reversal.reverse.assert_not_awaited()
    async with delivery_env.transactions() as session:
        intent = await session.scalar(
            select(DeliveryIntent).where(
                DeliveryIntent.operation_id == operation_id.value
            )
        )
        assert intent is not None
        assert intent.state == "delivered"
        assert intent.attempt_count == 1


async def test_timeout_requires_explicit_resend_and_never_charges_again(
    delivery_env: DeliveryEnvironment,
) -> None:
    operation_id = await _operation(delivery_env)
    await _prepare_and_capture(delivery_env, operation_id)
    delivery_env.bot.send_photo.side_effect = [TimeoutError(), _message(202)]

    uncertain = await delivery_env.service.deliver(operation_id)
    blocked = await delivery_env.service.deliver(operation_id)
    resent = await delivery_env.service.deliver(operation_id, explicit_resend=True)

    assert isinstance(uncertain, DeliveryUncertain)
    assert blocked == DeliveryUncertain("explicit_resend_required")
    assert resent == Delivered((202,))
    assert delivery_env.bot.send_photo.await_count == 2
    delivery_env.reversal.reverse.assert_not_awaited()


async def test_definite_telegram_rejection_reverses_captured_spend(
    delivery_env: DeliveryEnvironment,
) -> None:
    operation_id = await _operation(delivery_env)
    await _prepare_and_capture(delivery_env, operation_id)
    delivery_env.bot.send_photo.side_effect = TelegramBadRequest(
        MagicMock(), "chat not found"
    )

    outcome = await delivery_env.service.deliver(operation_id)

    assert outcome == DeliveryFailed("TelegramBadRequest", retryable=False)
    delivery_env.reversal.reverse.assert_awaited_once_with(
        operation_id,
        reason="delivery_TelegramBadRequest",
    )


async def test_expired_artifact_reverses_without_sending(
    delivery_env: DeliveryEnvironment,
) -> None:
    operation_id = await _operation(delivery_env)
    await _prepare_and_capture(delivery_env, operation_id)
    delivery_env.clock.now += timedelta(hours=1)

    outcome = await delivery_env.service.deliver(operation_id)

    assert outcome == DeliveryFailed("artifact_expired", retryable=False)
    delivery_env.bot.send_photo.assert_not_awaited()
    delivery_env.reversal.reverse.assert_awaited_once()


async def test_delivery_cannot_open_before_spend_capture(
    delivery_env: DeliveryEnvironment,
) -> None:
    operation_id = await _operation(delivery_env)
    await delivery_env.service.persist_result(
        operation_id,
        media=(MediaContent(MediaFamily.IMAGE, "image/png", b"image"),),
        target=DeliveryTarget(-1001, None, 42),
    )

    with pytest.raises(DeliveryStateError, match="before spend capture"):
        await delivery_env.service.mark_ready(operation_id)
