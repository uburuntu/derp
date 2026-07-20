"""Durable artifact preparation and conservative Telegram delivery."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import logfire
from aiogram import Bot
from aiogram.types import BufferedInputFile, InputMediaPhoto, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.artifacts import (
    ArtifactKey,
    ArtifactKind,
    ArtifactMetadata,
    ArtifactStore,
    ArtifactStoreError,
)
from derp.common.sanitize import sanitize_for_telegram
from derp.delivery.types import (
    Delivered,
    DeliveryFailed,
    DeliveryOutcome,
    DeliveryTarget,
    DeliveryUncertain,
    classify_delivery_exception,
)
from derp.features import MediaContent
from derp.media import MediaFamily
from derp.models import Artifact, DeliveryIntent, PaidOperation
from derp.operations import OperationId, OperationState

type TransactionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class SpendReversal(Protocol):
    """Settlement boundary used only after a terminal delivery failure."""

    async def reverse(self, operation_id: OperationId, *, reason: str) -> object: ...


class DeliveryStateError(RuntimeError):
    """A delivery transition violated its durable lifecycle."""


@dataclass(frozen=True, slots=True)
class PreparedDelivery:
    """Persisted artifacts plus the recoverable resend identity."""

    operation_id: OperationId
    intent_id: uuid.UUID
    resend_token: str
    artifact_count: int
    idempotent: bool = False


@dataclass(frozen=True, slots=True)
class ArtifactDescriptor:
    """Database metadata required to load and render one artifact."""

    key: ArtifactKey
    kind: ArtifactKind
    mime_type: str
    filename: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class DeliveryAttempt:
    """Committed send attempt safe to execute outside a transaction."""

    operation_id: OperationId
    target: DeliveryTarget
    caption: str | None
    artifacts: tuple[ArtifactDescriptor, ...]


type BeginDeliveryResult = DeliveryAttempt | DeliveryOutcome


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DeliveryService:
    """Coordinate persisted delivery state around one non-idempotent Bot API call."""

    def __init__(
        self,
        transactions: TransactionFactory,
        artifact_store: ArtifactStore,
        bot: Bot,
        spend_reversal: SpendReversal,
        *,
        clock: Callable[[], datetime] = _utc_now,
        artifact_ttl: timedelta = timedelta(hours=6),
    ) -> None:
        if artifact_ttl <= timedelta(0):
            raise ValueError("artifact_ttl must be positive")
        self._transactions = transactions
        self._artifact_store = artifact_store
        self._bot = bot
        self._spend_reversal = spend_reversal
        self._clock = clock
        self._artifact_ttl = artifact_ttl

    async def persist_result(
        self,
        operation_id: OperationId,
        *,
        media: tuple[MediaContent, ...],
        target: DeliveryTarget,
        caption: str | None = None,
    ) -> PreparedDelivery:
        """Store provider output before capture without opening delivery yet."""
        if not media:
            raise ValueError("delivery result must contain media")
        if any(item.family is not MediaFamily.IMAGE for item in media):
            raise ValueError("the first delivery slice accepts image output only")
        normalized_caption = caption.strip() if caption else None
        if caption is not None and not normalized_caption:
            raise ValueError("caption must not be blank")

        existing = await self._existing_preparation(
            operation_id, target, normalized_caption
        )
        if existing is not None:
            return existing

        stored: list[ArtifactMetadata] = []
        try:
            for item in media:
                stored.append(
                    await self._artifact_store.put(
                        kind=ArtifactKind.IMAGE,
                        mime_type=item.mime_type,
                        data=item.data,
                    )
                )

            now = self._aware_now()
            expires_at = now + self._artifact_ttl
            intent_id = uuid.uuid4()
            resend_token = str(intent_id)
            async with self._transactions() as session:
                operation = await session.scalar(
                    select(PaidOperation)
                    .where(PaidOperation.id == operation_id.value)
                    .with_for_update()
                )
                if operation is None:
                    raise DeliveryStateError(f"Unknown operation {operation_id}")
                if operation.state not in {
                    OperationState.EXECUTING.value,
                    OperationState.CAPTURED.value,
                }:
                    raise DeliveryStateError(
                        f"Cannot persist output for operation in {operation.state}"
                    )
                for ordinal, metadata in enumerate(stored):
                    session.add(
                        Artifact(
                            operation_id=operation.id,
                            ordinal=ordinal,
                            kind=metadata.kind.value,
                            mime_type=metadata.mime_type,
                            filename=self._filename(metadata.mime_type, ordinal),
                            storage_key=metadata.key.value.hex,
                            size_bytes=metadata.size_bytes,
                            sha256=metadata.sha256,
                            expires_at=expires_at,
                            created_at=now,
                        )
                    )
                session.add(
                    DeliveryIntent(
                        id=intent_id,
                        operation_id=operation.id,
                        chat_id=target.chat_id,
                        thread_id=target.thread_id,
                        reply_to_message_id=target.reply_to_message_id,
                        business_connection_id=target.business_connection_id,
                        resend_token_hash=self._token_hash(resend_token),
                        state="not_ready",
                        expires_at=expires_at,
                        caption=normalized_caption,
                    )
                )
                operation.result_metadata = {
                    **operation.result_metadata,
                    "artifact_count": len(stored),
                }
            return PreparedDelivery(
                operation_id,
                intent_id,
                resend_token,
                len(stored),
            )
        except Exception:
            for metadata in stored:
                try:
                    await self._artifact_store.delete(metadata.key)
                except ArtifactStoreError:
                    logfire.warning(
                        "artifact_orphan_cleanup_failed",
                        operation_id=str(operation_id),
                        artifact_key_fingerprint=hashlib.sha256(
                            str(metadata.key).encode()
                        ).hexdigest()[:12],
                    )
            raise

    async def mark_ready(self, operation_id: OperationId) -> None:
        """Open delivery only after the operation's spend has been captured."""
        async with self._transactions() as session:
            operation, intent = await self._locked_state(session, operation_id)
            if intent.state in {"pending", "delivering", "delivered", "uncertain"}:
                return
            if intent.state != "not_ready":
                raise DeliveryStateError(f"Cannot ready delivery in {intent.state}")
            if operation.state != OperationState.CAPTURED.value:
                raise DeliveryStateError(
                    "Delivery cannot become ready before spend capture"
                )
            intent.state = "pending"

    async def deliver(
        self,
        operation_id: OperationId,
        *,
        explicit_resend: bool = False,
    ) -> DeliveryOutcome:
        """Attempt delivery once; uncertain attempts require explicit user retry."""
        started = await self._begin_attempt(
            operation_id, explicit_resend=explicit_resend
        )
        if not isinstance(started, DeliveryAttempt):
            if isinstance(started, DeliveryFailed) and not started.retryable:
                await self._reverse_terminal(operation_id, started.code)
            return started

        try:
            loaded_artifacts = []
            for descriptor in started.artifacts:
                loaded_artifacts.append(await self._artifact_store.read(descriptor.key))
            artifacts = tuple(loaded_artifacts)
        except ArtifactStoreError as exc:
            outcome: DeliveryOutcome = DeliveryFailed(type(exc).__name__, False)
        else:
            try:
                messages = await self._send_images(started, artifacts)
                outcome = Delivered(tuple(message.message_id for message in messages))
            except BaseException as exc:
                outcome = classify_delivery_exception(exc)

        settled = await self._complete_attempt(operation_id, outcome)
        if isinstance(settled, DeliveryFailed) and not settled.retryable:
            await self._reverse_terminal(operation_id, settled.code)
        return settled

    async def _begin_attempt(
        self,
        operation_id: OperationId,
        *,
        explicit_resend: bool,
    ) -> BeginDeliveryResult:
        now = self._aware_now()
        async with self._transactions() as session:
            _, intent = await self._locked_state(session, operation_id)
            if intent.state == "delivered":
                return Delivered(tuple(intent.telegram_message_ids))
            if now >= intent.expires_at:
                intent.state = "expired"
                intent.failed_at = now
                intent.last_error_code = "artifact_expired"
                return DeliveryFailed("artifact_expired", retryable=False)
            if intent.state == "uncertain" and not explicit_resend:
                return DeliveryUncertain("explicit_resend_required")
            if intent.state == "delivering":
                return DeliveryUncertain("attempt_in_progress_or_interrupted")
            if intent.state in {"failed", "expired", "not_ready"}:
                return DeliveryFailed(
                    intent.state,
                    retryable=intent.state == "not_ready",
                )
            if intent.state not in {"pending", "uncertain"}:
                raise DeliveryStateError(f"Cannot deliver intent in {intent.state}")

            rows = list(
                await session.scalars(
                    select(Artifact)
                    .where(Artifact.operation_id == operation_id.value)
                    .order_by(Artifact.ordinal)
                )
            )
            if not rows:
                intent.state = "failed"
                intent.failed_at = now
                intent.last_error_code = "artifact_missing"
                return DeliveryFailed("artifact_missing", retryable=False)

            intent.state = "delivering"
            intent.attempt_count += 1
            intent.uncertain_at = None
            intent.failed_at = None
            intent.last_error_code = None
            return DeliveryAttempt(
                operation_id=operation_id,
                target=DeliveryTarget(
                    intent.chat_id,
                    intent.thread_id,
                    intent.reply_to_message_id,
                    intent.business_connection_id,
                ),
                caption=intent.caption,
                artifacts=tuple(
                    ArtifactDescriptor(
                        key=ArtifactKey(uuid.UUID(row.storage_key)),
                        kind=ArtifactKind(row.kind),
                        mime_type=row.mime_type,
                        filename=row.filename,
                        size_bytes=row.size_bytes,
                        sha256=row.sha256,
                    )
                    for row in rows
                ),
            )

    async def _complete_attempt(
        self,
        operation_id: OperationId,
        outcome: DeliveryOutcome,
    ) -> DeliveryOutcome:
        now = self._aware_now()
        async with self._transactions() as session:
            _, intent = await self._locked_state(session, operation_id)
            if intent.state == "delivered":
                return Delivered(tuple(intent.telegram_message_ids))
            if intent.state != "delivering":
                raise DeliveryStateError(f"Cannot complete delivery in {intent.state}")
            if isinstance(outcome, Delivered):
                intent.state = "delivered"
                intent.delivered_at = now
                intent.telegram_message_ids = list(outcome.message_ids)
            elif isinstance(outcome, DeliveryUncertain):
                intent.state = "uncertain"
                intent.uncertain_at = now
                intent.last_error_code = outcome.code
            elif outcome.retryable:
                intent.state = "pending"
                intent.last_error_code = outcome.code
            else:
                intent.state = "failed"
                intent.failed_at = now
                intent.last_error_code = outcome.code
            return outcome

    async def _existing_preparation(
        self,
        operation_id: OperationId,
        target: DeliveryTarget,
        caption: str | None,
    ) -> PreparedDelivery | None:
        async with self._transactions() as session:
            intent = await session.scalar(
                select(DeliveryIntent).where(
                    DeliveryIntent.operation_id == operation_id.value
                )
            )
            if intent is None:
                return None
            stored_target = DeliveryTarget(
                intent.chat_id,
                intent.thread_id,
                intent.reply_to_message_id,
                intent.business_connection_id,
            )
            if stored_target != target or intent.caption != caption:
                raise DeliveryStateError("Conflicting delivery target for operation")
            count = int(
                await session.scalar(
                    select(func.count(Artifact.id)).where(
                        Artifact.operation_id == operation_id.value
                    )
                )
                or 0
            )
            if count == 0:
                raise DeliveryStateError("Delivery intent has no artifacts")
            return PreparedDelivery(
                operation_id,
                intent.id,
                str(intent.id),
                count,
                idempotent=True,
            )

    async def _locked_state(
        self,
        session: AsyncSession,
        operation_id: OperationId,
    ) -> tuple[PaidOperation, DeliveryIntent]:
        operation = await session.scalar(
            select(PaidOperation)
            .where(PaidOperation.id == operation_id.value)
            .with_for_update()
        )
        intent = await session.scalar(
            select(DeliveryIntent)
            .where(DeliveryIntent.operation_id == operation_id.value)
            .with_for_update()
        )
        if operation is None or intent is None:
            raise DeliveryStateError(f"Unknown delivery for {operation_id}")
        return operation, intent

    async def _send_images(self, attempt, artifacts) -> tuple[Message, ...]:
        caption = sanitize_for_telegram(attempt.caption) if attempt.caption else None
        common = {
            "chat_id": attempt.target.chat_id,
            "message_thread_id": attempt.target.thread_id,
            "business_connection_id": attempt.target.business_connection_id,
            "reply_to_message_id": attempt.target.reply_to_message_id,
        }
        if len(artifacts) == 1:
            artifact = artifacts[0]
            message = await self._bot.send_photo(
                **common,
                photo=BufferedInputFile(
                    artifact.data,
                    filename=attempt.artifacts[0].filename,
                ),
                caption=caption,
                parse_mode="HTML" if caption else None,
            )
            return (message,)

        media = [
            InputMediaPhoto(
                media=BufferedInputFile(
                    artifact.data,
                    filename=descriptor.filename,
                ),
                caption=caption if index == 0 else None,
                parse_mode="HTML" if index == 0 and caption else None,
            )
            for index, (descriptor, artifact) in enumerate(
                zip(attempt.artifacts, artifacts, strict=True)
            )
        ]
        messages = await self._bot.send_media_group(**common, media=media)
        return tuple(messages)

    async def _reverse_terminal(
        self, operation_id: OperationId, error_code: str
    ) -> None:
        await self._spend_reversal.reverse(
            operation_id,
            reason=f"delivery_{error_code}",
        )

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("DeliveryService clock must return an aware datetime")
        return now

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _filename(mime_type: str, ordinal: int) -> str:
        extension = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
        }.get(mime_type, "bin")
        return f"image_{ordinal + 1}.{extension}"
