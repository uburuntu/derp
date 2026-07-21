"""Database invariants for TTL artifacts and delivery reconciliation."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError

from derp.models import Artifact, DeliveryIntent, OperationQuote, PaidOperation

pytestmark = pytest.mark.database


async def _operation(db_session, user_factory, chat_factory) -> PaidOperation:
    user = await user_factory(telegram_id=9_400_001 + uuid4().int % 100_000)
    chat = await chat_factory(telegram_id=-(9_400_001 + uuid4().int % 100_000))
    operation_id = uuid4()
    quote = OperationQuote(
        operation_id=operation_id,
        request_key=f"delivery:{operation_id}",
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
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    db_session.add(quote)
    await db_session.flush()
    operation = PaidOperation(id=operation_id, quote_id=quote.id)
    db_session.add(operation)
    await db_session.flush()
    return operation


async def test_artifact_and_delivery_intent_preserve_recoverable_result(
    db_session, user_factory, chat_factory
) -> None:
    operation = await _operation(db_session, user_factory, chat_factory)
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    artifact = Artifact(
        operation_id=operation.id,
        ordinal=0,
        kind="image",
        mime_type="image/png",
        filename="result.png",
        storage_key=uuid4().hex,
        size_bytes=3,
        sha256="a" * 64,
        expires_at=expires_at,
    )
    intent = DeliveryIntent(
        operation_id=operation.id,
        chat_id=-1001,
        thread_id=77,
        reply_to_message_id=42,
        resend_token_hash="b" * 64,
        expires_at=expires_at,
    )
    db_session.add_all([artifact, intent])
    await db_session.flush()
    await db_session.refresh(operation)

    assert operation.delivery_state == "not_ready"
    assert artifact.operation_id == intent.operation_id


async def test_delivery_state_syncs_to_operation_on_acknowledgement(
    db_session, user_factory, chat_factory
) -> None:
    operation = await _operation(db_session, user_factory, chat_factory)
    now = datetime.now(UTC)
    intent = DeliveryIntent(
        operation_id=operation.id,
        chat_id=1,
        resend_token_hash="c" * 64,
        expires_at=now + timedelta(hours=1),
    )
    db_session.add(intent)
    await db_session.flush()
    intent.state = "delivered"
    intent.telegram_message_ids = [101, 102]
    intent.delivered_at = now
    await db_session.flush()
    await db_session.refresh(operation)

    assert operation.delivery_state == "delivered"
    assert intent.telegram_message_ids == [101, 102]


async def test_delivery_terminal_state_requires_matching_timestamp(
    db_session, user_factory, chat_factory
) -> None:
    operation = await _operation(db_session, user_factory, chat_factory)
    intent = DeliveryIntent(
        operation_id=operation.id,
        chat_id=1,
        resend_token_hash="d" * 64,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        state="delivered",
    )
    db_session.add(intent)

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_delivery_target_is_immutable_but_progress_is_mutable(
    db_session, user_factory, chat_factory
) -> None:
    operation = await _operation(db_session, user_factory, chat_factory)
    intent = DeliveryIntent(
        operation_id=operation.id,
        chat_id=1,
        reply_to_message_id=2,
        resend_token_hash="e" * 64,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(intent)
    await db_session.flush()
    intent.progress_message_id = 3
    await db_session.flush()
    intent.reply_to_message_id = 4

    with pytest.raises(DBAPIError, match="delivery target is immutable"):
        await db_session.flush()


async def test_artifact_metadata_is_database_immutable(
    db_session, user_factory, chat_factory
) -> None:
    operation = await _operation(db_session, user_factory, chat_factory)
    artifact = Artifact(
        operation_id=operation.id,
        ordinal=0,
        kind="image",
        mime_type="image/png",
        filename="result.png",
        storage_key=uuid4().hex,
        size_bytes=3,
        sha256="f" * 64,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(artifact)
    await db_session.flush()
    artifact.filename = "changed.png"

    with pytest.raises(DBAPIError, match="artifact metadata is immutable"):
        await db_session.flush()


async def test_voice_is_a_first_class_durable_artifact_kind(
    db_session, user_factory, chat_factory
) -> None:
    operation = await _operation(db_session, user_factory, chat_factory)
    artifact = Artifact(
        operation_id=operation.id,
        ordinal=0,
        kind="voice",
        mime_type="audio/ogg",
        filename="voice_1.ogg",
        storage_key=uuid4().hex,
        size_bytes=5,
        sha256="0" * 64,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    db_session.add(artifact)

    await db_session.flush()

    assert artifact.kind == "voice"


async def test_terminal_delivery_allows_one_way_caption_scrub(
    db_session, user_factory, chat_factory
) -> None:
    operation = await _operation(db_session, user_factory, chat_factory)
    now = datetime.now(UTC)
    intent = DeliveryIntent(
        operation_id=operation.id,
        chat_id=1,
        resend_token_hash="1" * 64,
        expires_at=now + timedelta(hours=1),
        caption="temporary result caption",
    )
    db_session.add(intent)
    await db_session.flush()

    intent.state = "delivered"
    intent.delivered_at = now
    intent.telegram_message_ids = [101]
    intent.caption = None
    await db_session.flush()

    assert intent.caption is None
