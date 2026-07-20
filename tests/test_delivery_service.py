"""Durable delivery never guesses whether Telegram accepted a result."""

from __future__ import annotations

import asyncio
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

from derp.artifacts import ArtifactTooLargeError, FilesystemArtifactStore
from derp.delivery import (
    MAX_TELEGRAM_PHOTO_BYTES,
    Delivered,
    DeliveryAuthorizationError,
    DeliveryFailed,
    DeliveryService,
    DeliveryState,
    DeliveryStateError,
    DeliveryTarget,
    DeliveryUncertain,
    PreparedDelivery,
    ResendAuthorization,
    ResendCallbackAuthorization,
    ResendResult,
    ResendTokenCodec,
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
        ResendTokenCodec(b"delivery-test-secret".ljust(32, b"!")),
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
) -> PreparedDelivery:
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
    assert duplicate.resend_token == prepared.resend_token
    assert prepared.resend_token != str(prepared.intent_id)
    async with env.transactions() as session:
        operation = await session.get(PaidOperation, operation_id.value)
        assert operation is not None
        operation.state = "captured"
        operation.captured_at = env.clock()
    await env.service.mark_ready(operation_id)
    return prepared


async def _requester_telegram_id(
    env: DeliveryEnvironment,
    operation_id: OperationId,
) -> int:
    async with env.transactions() as session:
        telegram_id = await session.scalar(
            select(User.telegram_id)
            .join(OperationQuote, OperationQuote.requester_id == User.id)
            .join(PaidOperation, PaidOperation.quote_id == OperationQuote.id)
            .where(PaidOperation.id == operation_id.value)
        )
    assert telegram_id is not None
    return telegram_id


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
    inspection = await delivery_env.service.inspect(operation_id)
    assert inspection.state is DeliveryState.DELIVERED
    assert inspection.artifact_count == 0
    async with delivery_env.transactions() as session:
        intent = await session.scalar(
            select(DeliveryIntent).where(
                DeliveryIntent.operation_id == operation_id.value
            )
        )
        assert intent is not None and intent.caption is None


async def test_timeout_requires_authenticated_resend_and_never_charges_again(
    delivery_env: DeliveryEnvironment,
) -> None:
    operation_id = await _operation(delivery_env)
    prepared = await _prepare_and_capture(delivery_env, operation_id)
    delivery_env.bot.send_photo.side_effect = [TimeoutError(), _message(202)]
    target = DeliveryTarget(-1001, 77, 42)
    actor_user_id = await _requester_telegram_id(delivery_env, operation_id)

    uncertain = await delivery_env.service.deliver(operation_id)
    blocked = await delivery_env.service.deliver(operation_id)
    resent = await delivery_env.service.resend(
        ResendAuthorization(prepared.resend_token, actor_user_id, target)
    )

    assert isinstance(uncertain, DeliveryUncertain)
    assert blocked == DeliveryUncertain("authenticated_resend_required")
    assert resent == Delivered((202,))
    assert delivery_env.bot.send_photo.await_count == 2
    delivery_env.reversal.reverse.assert_not_awaited()


async def test_callback_resend_resolves_original_operation_target_and_requester(
    delivery_env: DeliveryEnvironment,
) -> None:
    operation_id = await _operation(delivery_env)
    prepared = await _prepare_and_capture(delivery_env, operation_id)
    delivery_env.bot.send_photo.side_effect = [TimeoutError(), _message(203)]
    actor_user_id = await _requester_telegram_id(delivery_env, operation_id)
    await delivery_env.service.deliver(operation_id)

    result = await delivery_env.service.resend_from_callback(
        ResendCallbackAuthorization(
            prepared.resend_token,
            actor_user_id,
            chat_id=-1001,
            thread_id=77,
        )
    )

    assert result == ResendResult(
        operation_id,
        DeliveryTarget(-1001, 77, 42),
        Delivered((203,)),
    )
    assert delivery_env.bot.send_photo.await_count == 2
    delivery_env.reversal.reverse.assert_not_awaited()
    async with delivery_env.transactions() as session:
        intent = await session.scalar(
            select(DeliveryIntent).where(
                DeliveryIntent.operation_id == operation_id.value
            )
        )
        assert intent is not None
        assert intent.resend_token_hash != prepared.resend_token
        assert len(intent.resend_token_hash) == 64


async def test_callback_resend_definite_failure_refunds_without_another_charge(
    delivery_env: DeliveryEnvironment,
) -> None:
    operation_id = await _operation(delivery_env)
    prepared = await _prepare_and_capture(delivery_env, operation_id)
    delivery_env.bot.send_photo.side_effect = [
        TimeoutError(),
        TelegramBadRequest(MagicMock(), "chat not found"),
    ]
    actor_user_id = await _requester_telegram_id(delivery_env, operation_id)
    await delivery_env.service.deliver(operation_id)

    result = await delivery_env.service.resend_from_callback(
        ResendCallbackAuthorization(
            prepared.resend_token,
            actor_user_id,
            chat_id=-1001,
            thread_id=77,
        )
    )

    assert result.outcome == DeliveryFailed(
        "TelegramBadRequest",
        retryable=False,
    )
    assert delivery_env.bot.send_photo.await_count == 2
    delivery_env.reversal.reverse.assert_awaited_once_with(
        operation_id,
        reason="delivery_TelegramBadRequest",
    )


async def test_uncertain_delivery_can_reissue_restart_stable_resend_token(
    delivery_env: DeliveryEnvironment,
) -> None:
    operation_id = await _operation(delivery_env)
    prepared = await _prepare_and_capture(delivery_env, operation_id)
    delivery_env.bot.send_photo.side_effect = TimeoutError()

    await delivery_env.service.deliver(operation_id)

    assert (
        await delivery_env.service.issue_resend_token(operation_id)
        == prepared.resend_token
    )


async def test_resend_token_is_not_issued_for_an_unambiguous_delivery(
    delivery_env: DeliveryEnvironment,
) -> None:
    operation_id = await _operation(delivery_env)
    await _prepare_and_capture(delivery_env, operation_id)

    with pytest.raises(DeliveryStateError, match="pending"):
        await delivery_env.service.issue_resend_token(operation_id)


async def test_resend_rejects_forged_token_actor_and_target(
    delivery_env: DeliveryEnvironment,
) -> None:
    operation_id = await _operation(delivery_env)
    prepared = await _prepare_and_capture(delivery_env, operation_id)
    delivery_env.bot.send_photo.side_effect = TimeoutError()
    actor_user_id = await _requester_telegram_id(delivery_env, operation_id)
    target = DeliveryTarget(-1001, 77, 42)
    await delivery_env.service.deliver(operation_id)

    authorizations = (
        ResendAuthorization(str(uuid4()), actor_user_id, target),
        ResendAuthorization(prepared.resend_token, actor_user_id + 1, target),
        ResendAuthorization(
            prepared.resend_token,
            actor_user_id,
            DeliveryTarget(-1002, 77, 42),
        ),
    )
    for authorization in authorizations:
        with pytest.raises(
            DeliveryAuthorizationError,
            match="Invalid resend authorization",
        ):
            await delivery_env.service.resend(authorization)

    assert delivery_env.bot.send_photo.await_count == 1


async def test_callback_resend_rejects_forged_actor_chat_and_topic(
    delivery_env: DeliveryEnvironment,
) -> None:
    operation_id = await _operation(delivery_env)
    prepared = await _prepare_and_capture(delivery_env, operation_id)
    delivery_env.bot.send_photo.side_effect = TimeoutError()
    actor_user_id = await _requester_telegram_id(delivery_env, operation_id)
    await delivery_env.service.deliver(operation_id)

    authorizations = (
        ResendCallbackAuthorization(
            prepared.resend_token,
            actor_user_id + 1,
            chat_id=-1001,
            thread_id=77,
        ),
        ResendCallbackAuthorization(
            prepared.resend_token,
            actor_user_id,
            chat_id=-1002,
            thread_id=77,
        ),
        ResendCallbackAuthorization(
            prepared.resend_token,
            actor_user_id,
            chat_id=-1001,
            thread_id=78,
        ),
    )
    for authorization in authorizations:
        with pytest.raises(
            DeliveryAuthorizationError,
            match="Invalid resend authorization",
        ):
            await delivery_env.service.resend_from_callback(authorization)

    assert delivery_env.bot.send_photo.await_count == 1


async def test_concurrent_send_is_uncertain_and_cancellation_is_reraised(
    delivery_env: DeliveryEnvironment,
) -> None:
    operation_id = await _operation(delivery_env)
    await _prepare_and_capture(delivery_env, operation_id)
    send_started = asyncio.Event()

    async def wait_for_cancellation(**_kwargs) -> Message:
        send_started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    delivery_env.bot.send_photo.side_effect = wait_for_cancellation
    send_task = asyncio.create_task(delivery_env.service.deliver(operation_id))
    await asyncio.wait_for(send_started.wait(), timeout=1)

    concurrent = await delivery_env.service.deliver(operation_id)
    send_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await send_task

    assert concurrent == DeliveryUncertain("attempt_in_progress_or_interrupted")
    inspection = await delivery_env.service.inspect(operation_id)
    assert inspection.state is DeliveryState.UNCERTAIN
    assert inspection.last_error_code == "CancelledError"
    assert inspection.attempt_count == 1
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


async def test_photo_limit_is_enforced_before_artifact_persistence(
    delivery_env: DeliveryEnvironment,
) -> None:
    operation_id = await _operation(delivery_env)

    with pytest.raises(ArtifactTooLargeError) as error:
        await delivery_env.service.persist_result(
            operation_id,
            media=(
                MediaContent(
                    MediaFamily.IMAGE,
                    "image/png",
                    bytes(MAX_TELEGRAM_PHOTO_BYTES + 1),
                ),
            ),
            target=DeliveryTarget(-1001, None, 42),
        )

    assert error.value.limit_bytes == MAX_TELEGRAM_PHOTO_BYTES
    with pytest.raises(DeliveryStateError, match="Unknown delivery"):
        await delivery_env.service.inspect(operation_id)


async def test_startup_reconciliation_marks_interrupted_send_uncertain(
    delivery_env: DeliveryEnvironment,
) -> None:
    operation_id = await _operation(delivery_env)
    await _prepare_and_capture(delivery_env, operation_id)
    interrupted_at = delivery_env.clock() - timedelta(minutes=5)
    async with delivery_env.transactions() as session:
        intent = await session.scalar(
            select(DeliveryIntent).where(
                DeliveryIntent.operation_id == operation_id.value
            )
        )
        assert intent is not None
        intent.state = DeliveryState.DELIVERING.value
        intent.attempt_count = 1
        intent.updated_at = interrupted_at

    report = await delivery_env.service.reconcile_interrupted(
        stale_before=delivery_env.clock() - timedelta(minutes=1)
    )

    assert operation_id in report.operation_ids
    inspection = await delivery_env.service.inspect(operation_id)
    assert inspection.state is DeliveryState.UNCERTAIN
    assert inspection.last_error_code == "process_interrupted"
    delivery_env.bot.send_photo.assert_not_awaited()


async def test_expiry_reconciliation_reverses_and_cleans_without_sending(
    delivery_env: DeliveryEnvironment,
) -> None:
    operation_id = await _operation(delivery_env)
    delivery_env.clock.now = datetime(2020, 1, 1, tzinfo=UTC)
    await _prepare_and_capture(delivery_env, operation_id)
    delivery_env.clock.now += timedelta(hours=1)

    report = await delivery_env.service.reconcile_expired()

    assert operation_id in report.operation_ids
    delivery_env.bot.send_photo.assert_not_awaited()
    delivery_env.reversal.reverse.assert_any_await(
        operation_id,
        reason="delivery_artifact_expired",
    )
    inspection = await delivery_env.service.inspect(operation_id)
    assert inspection.state is DeliveryState.EXPIRED
    assert inspection.artifact_count == 0


async def test_expired_artifact_cleanup_is_bounded_to_terminal_deliveries(
    delivery_env: DeliveryEnvironment,
) -> None:
    operation_id = await _operation(delivery_env)
    delivery_env.clock.now = datetime(2020, 1, 1, tzinfo=UTC)
    await _prepare_and_capture(delivery_env, operation_id)
    async with delivery_env.transactions() as session:
        intent = await session.scalar(
            select(DeliveryIntent).where(
                DeliveryIntent.operation_id == operation_id.value
            )
        )
        assert intent is not None
        intent.state = DeliveryState.DELIVERED.value
        intent.delivered_at = delivery_env.clock()
    delivery_env.clock.now += timedelta(hours=1)

    cleanup = await delivery_env.service.cleanup_expired_artifacts(limit=1000)

    assert cleanup.examined_count == cleanup.purged_count
    assert cleanup.purged_count >= 1
    assert cleanup.failed_count == 0
    assert (await delivery_env.service.inspect(operation_id)).artifact_count == 0
