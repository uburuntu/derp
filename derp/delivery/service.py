"""Durable artifact preparation and conservative Telegram delivery."""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import logfire
from aiogram import Bot
from aiogram.types import (
    BufferedInputFile,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from derp.artifacts import (
    ArtifactIntegrityError,
    ArtifactKey,
    ArtifactKind,
    ArtifactMetadata,
    ArtifactStore,
    ArtifactStoreError,
    StoredArtifact,
)
from derp.common.sanitize import sanitize_for_telegram
from derp.delivery.tokens import ResendTokenCodec
from derp.delivery.types import (
    MAX_TELEGRAM_PHOTO_BYTES as MAX_TELEGRAM_PHOTO_BYTES,
)
from derp.delivery.types import (
    ArtifactCleanup,
    Delivered,
    DeliveryBatchKind,
    DeliveryFailed,
    DeliveryInspection,
    DeliveryMedia,
    DeliveryOutcome,
    DeliveryReconciliation,
    DeliveryState,
    DeliveryTarget,
    DeliveryUncertain,
    ResendAuthorization,
    ResendCallbackAuthorization,
    ResendResult,
    TelegramMediaKind,
    classify_delivery_batch,
    classify_delivery_exception,
)
from derp.features import MediaContent
from derp.media import MediaFamily
from derp.models import (
    Artifact,
    DeliveryIntent,
    OperationQuote,
    PaidOperation,
    User,
)
from derp.observability import report_exception
from derp.operations import OperationId, OperationState

type TransactionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

_SAFE_FILENAME_PATTERN = re.compile(
    r"^(?:image|video|audio|voice|document)_[1-9][0-9]*\.[a-z0-9]{1,8}$"
)


class SpendReversal(Protocol):
    """Settlement boundary used only after a terminal delivery failure."""

    async def reverse(self, operation_id: OperationId, *, reason: str) -> object: ...


class DeliveryStateError(RuntimeError):
    """A delivery transition violated its durable lifecycle."""


class DeliveryAuthorizationError(PermissionError):
    """A resend capability does not match its actor and Telegram scope."""


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
    kind: TelegramMediaKind
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


@dataclass(frozen=True, slots=True)
class _ArtifactCleanupCandidate:
    artifact_id: uuid.UUID
    key: ArtifactKey


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
        token_codec: ResendTokenCodec,
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
        self._token_codec = token_codec
        self._clock = clock
        self._artifact_ttl = artifact_ttl

    async def persist_result(
        self,
        operation_id: OperationId,
        *,
        media: tuple[MediaContent | DeliveryMedia, ...],
        target: DeliveryTarget,
        caption: str | None = None,
    ) -> PreparedDelivery:
        """Store provider output before capture without opening delivery yet."""
        delivery_media = tuple(self._coerce_media(item) for item in media)
        classify_delivery_batch(tuple(item.kind for item in delivery_media))
        normalized_caption = caption.strip() if caption else None
        if caption is not None and not normalized_caption:
            raise ValueError("caption must not be blank")

        existing = await self._existing_preparation(
            operation_id,
            target,
            normalized_caption,
            delivery_media,
        )
        if existing is not None:
            return existing

        persistence = asyncio.create_task(
            self._persist_new_result(
                operation_id,
                delivery_media=delivery_media,
                target=target,
                caption=normalized_caption,
            ),
            name=f"persist-delivery-{operation_id}",
        )
        try:
            return await asyncio.shield(persistence)
        except asyncio.CancelledError:
            # Once a file write starts, let the filesystem/DB unit finish or clean
            # itself so cancellation cannot leave an undiscoverable private blob.
            with suppress(Exception, asyncio.CancelledError):
                await persistence
            raise

    async def _persist_new_result(
        self,
        operation_id: OperationId,
        *,
        delivery_media: tuple[DeliveryMedia, ...],
        target: DeliveryTarget,
        caption: str | None,
    ) -> PreparedDelivery:
        stored: list[ArtifactMetadata] = []
        try:
            for item in delivery_media:
                stored.append(
                    await self._artifact_store.put(
                        kind=item.artifact_kind,
                        mime_type=item.mime_type,
                        data=item.data,
                    )
                )

            now = self._aware_now()
            expires_at = now + self._artifact_ttl
            intent_id = uuid.uuid4()
            resend_token = self._token_codec.issue(intent_id)
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
                durable_metadata: list[dict[str, int | str]] = []
                for ordinal, (item, metadata) in enumerate(
                    zip(delivery_media, stored, strict=True)
                ):
                    filename = self._filename(item.kind, metadata.mime_type, ordinal)
                    session.add(
                        Artifact(
                            operation_id=operation.id,
                            ordinal=ordinal,
                            kind=metadata.kind.value,
                            mime_type=metadata.mime_type,
                            filename=filename,
                            storage_key=metadata.key.value.hex,
                            size_bytes=metadata.size_bytes,
                            sha256=metadata.sha256,
                            expires_at=expires_at,
                            created_at=now,
                        )
                    )
                    durable_metadata.append(
                        {
                            "kind": item.kind.value,
                            "mime_type": metadata.mime_type,
                            "filename": filename,
                            "size_bytes": metadata.size_bytes,
                            "sha256": metadata.sha256,
                        }
                    )
                session.add(
                    DeliveryIntent(
                        id=intent_id,
                        operation_id=operation.id,
                        chat_id=target.chat_id,
                        thread_id=target.thread_id,
                        reply_to_message_id=target.reply_to_message_id,
                        business_connection_id=target.business_connection_id,
                        resend_token_hash=self._token_codec.digest(resend_token),
                        state="not_ready",
                        expires_at=expires_at,
                        caption=caption,
                        created_at=now,
                        updated_at=now,
                    )
                )
                operation.result_metadata = {
                    **operation.result_metadata,
                    "artifact_count": len(stored),
                    "delivery_artifacts": durable_metadata,
                }
            return PreparedDelivery(
                operation_id,
                intent_id,
                resend_token,
                len(stored),
            )
        except BaseException:
            await asyncio.shield(
                self._delete_uncommitted_artifacts(operation_id, stored)
            )
            raise

    async def _delete_uncommitted_artifacts(
        self,
        operation_id: OperationId,
        stored: list[ArtifactMetadata],
    ) -> None:
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

    async def mark_ready(self, operation_id: OperationId) -> bool:
        """Open delivery only after the operation's spend has been captured."""
        async with self._transactions() as session:
            operation, intent = await self._locked_state(session, operation_id)
            if intent.state in {"pending", "delivering", "delivered", "uncertain"}:
                operation.delivery_state = intent.state
                return False
            if intent.state != "not_ready":
                raise DeliveryStateError(f"Cannot ready delivery in {intent.state}")
            if operation.state != OperationState.CAPTURED.value:
                raise DeliveryStateError(
                    "Delivery cannot become ready before spend capture"
                )
            intent.state = "pending"
            operation.delivery_state = DeliveryState.PENDING.value
            return True

    async def reconcile_ready(self, operation_id: OperationId) -> bool:
        """Repair a committed result only while it still needs readiness."""
        async with self._transactions() as session:
            operation, intent = await self._locked_state(session, operation_id)
            if (
                intent.state != DeliveryState.NOT_READY.value
                or operation.state != OperationState.CAPTURED.value
            ):
                return False
            intent.state = DeliveryState.PENDING.value
            operation.delivery_state = DeliveryState.PENDING.value
            return True

    async def deliver(
        self,
        operation_id: OperationId,
    ) -> DeliveryOutcome:
        """Attempt initial delivery once without crossing an uncertainty gap."""
        started = await self._begin_attempt(operation_id)
        return await self._execute_attempt(operation_id, started)

    async def resend(
        self,
        authorization: ResendAuthorization,
    ) -> DeliveryOutcome:
        """Resend only for the original requester, target, and opaque capability."""
        operation_id, started = await self._begin_resend(authorization)
        return await self._execute_attempt(operation_id, started)

    async def resend_from_callback(
        self,
        authorization: ResendCallbackAuthorization,
    ) -> ResendResult:
        """Resend in the authenticated scope without opening another charge."""
        operation_id, target, started = await self._begin_callback_resend(authorization)
        outcome = await self._execute_attempt(operation_id, started)
        return ResendResult(operation_id, target, outcome)

    async def inspect(self, operation_id: OperationId) -> DeliveryInspection:
        """Return content-free durable state without changing delivery."""
        async with self._transactions() as session:
            intent = await session.scalar(
                select(DeliveryIntent).where(
                    DeliveryIntent.operation_id == operation_id.value
                )
            )
            if intent is None:
                raise DeliveryStateError(f"Unknown delivery for {operation_id}")
            artifact_count = int(
                await session.scalar(
                    select(func.count(Artifact.id)).where(
                        Artifact.operation_id == operation_id.value
                    )
                )
                or 0
            )
            return self._inspection(intent, artifact_count)

    async def issue_resend_token(self, operation_id: OperationId) -> str:
        """Reissue the opaque capability for one still-recoverable uncertain send."""
        async with self._transactions() as session:
            intent = await session.scalar(
                select(DeliveryIntent)
                .where(DeliveryIntent.operation_id == operation_id.value)
                .with_for_update()
            )
            if intent is None:
                raise DeliveryStateError(f"Unknown delivery for {operation_id}")
            if intent.state != DeliveryState.UNCERTAIN.value:
                raise DeliveryStateError(
                    f"Cannot issue resend token for delivery in {intent.state}"
                )
            if self._aware_now() >= intent.expires_at:
                raise DeliveryStateError(
                    "Cannot issue resend token after artifact expiry"
                )
            return self._token_codec.issue(intent.id)

    async def reconcile_interrupted(
        self,
        *,
        stale_before: datetime,
        limit: int = 100,
    ) -> DeliveryReconciliation:
        """Mark pre-existing in-flight sends uncertain without retrying them."""
        self._require_aware(stale_before, "stale_before")
        self._require_limit(limit)
        now = self._aware_now()
        if stale_before > now:
            raise ValueError("stale_before must not be in the future")
        async with self._transactions() as session:
            intents = list(
                await session.scalars(
                    select(DeliveryIntent)
                    .where(
                        DeliveryIntent.state == DeliveryState.DELIVERING.value,
                        DeliveryIntent.updated_at <= stale_before,
                    )
                    .order_by(DeliveryIntent.updated_at, DeliveryIntent.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for intent in intents:
                intent.state = DeliveryState.UNCERTAIN.value
                intent.uncertain_at = now
                intent.last_error_code = "process_interrupted"
            operation_ids = tuple(
                OperationId(intent.operation_id) for intent in intents
            )
        return DeliveryReconciliation(operation_ids)

    async def pending_operation_ids(
        self,
        *,
        limit: int = 100,
    ) -> tuple[OperationId, ...]:
        """Return one bounded batch that is safe for an initial send attempt."""
        self._require_limit(limit)
        now = self._aware_now()
        async with self._transactions() as session:
            operation_ids = tuple(
                await session.scalars(
                    select(DeliveryIntent.operation_id)
                    .join(
                        PaidOperation,
                        PaidOperation.id == DeliveryIntent.operation_id,
                    )
                    .where(
                        DeliveryIntent.state == DeliveryState.PENDING.value,
                        DeliveryIntent.expires_at > now,
                        PaidOperation.state == OperationState.CAPTURED.value,
                    )
                    .order_by(DeliveryIntent.updated_at, DeliveryIntent.id)
                    .limit(limit)
                )
            )
        return tuple(OperationId(value) for value in operation_ids)

    async def reconcile_expired(
        self,
        *,
        limit: int = 100,
    ) -> DeliveryReconciliation:
        """Expire due deliveries and finish interrupted terminal reversals."""
        self._require_limit(limit)
        now = self._aware_now()
        due_states = {
            DeliveryState.NOT_READY.value,
            DeliveryState.PENDING.value,
            DeliveryState.UNCERTAIN.value,
        }
        async with self._transactions() as session:
            rows = list(
                (
                    await session.execute(
                        select(DeliveryIntent, PaidOperation)
                        .join(
                            PaidOperation,
                            PaidOperation.id == DeliveryIntent.operation_id,
                        )
                        .where(
                            (
                                (DeliveryIntent.expires_at <= now)
                                & DeliveryIntent.state.in_(due_states)
                            )
                            | (
                                DeliveryIntent.state.in_(
                                    {
                                        DeliveryState.FAILED.value,
                                        DeliveryState.EXPIRED.value,
                                    }
                                )
                                & (PaidOperation.state == OperationState.CAPTURED.value)
                            )
                        )
                        .order_by(DeliveryIntent.expires_at, DeliveryIntent.id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            operation_ids: list[OperationId] = []
            reversal_codes: dict[uuid.UUID, str] = {}
            for intent, operation in rows:
                operation_id = OperationId(intent.operation_id)
                operation_ids.append(operation_id)
                if intent.state in due_states:
                    intent.state = DeliveryState.EXPIRED.value
                    intent.uncertain_at = None
                    intent.failed_at = now
                    intent.last_error_code = "artifact_expired"
                if operation.state == OperationState.CAPTURED.value:
                    reversal_codes[operation_id.value] = (
                        intent.last_error_code or "terminal_delivery_failure"
                    )

        failed_count = 0
        for operation_id in operation_ids:
            operation_failed = False
            if error_code := reversal_codes.get(operation_id.value):
                try:
                    await self._reverse_terminal(operation_id, error_code)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    report_exception(
                        "delivery_maintenance_operation_failed",
                        exception=exc,
                        level="warning",
                        phase="spend_reversal",
                        failure_count=1,
                    )
                    operation_failed = True
            try:
                await self.cleanup_terminal_artifacts(operation_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                report_exception(
                    "delivery_maintenance_operation_failed",
                    exception=exc,
                    level="warning",
                    phase="terminal_artifact_cleanup",
                    failure_count=1,
                )
                operation_failed = True
            failed_count += operation_failed
        return DeliveryReconciliation(tuple(operation_ids), failed_count)

    async def cleanup_terminal_artifacts(
        self,
        operation_id: OperationId,
    ) -> ArtifactCleanup:
        """Delete recoverable bytes only after delivery reaches a terminal state."""
        async with self._transactions() as session:
            intent = await session.scalar(
                select(DeliveryIntent).where(
                    DeliveryIntent.operation_id == operation_id.value
                )
            )
            if intent is None:
                raise DeliveryStateError(f"Unknown delivery for {operation_id}")
            state = DeliveryState(intent.state)
            if not state.terminal:
                raise DeliveryStateError(
                    f"Cannot clean artifacts for delivery in {intent.state}"
                )
            candidates = tuple(
                self._cleanup_candidate(row)
                for row in await session.scalars(
                    select(Artifact)
                    .where(Artifact.operation_id == operation_id.value)
                    .order_by(Artifact.ordinal)
                )
            )
            intent.caption = None
        return await self._purge_artifacts(candidates)

    async def cleanup_expired_artifacts(
        self,
        *,
        limit: int = 100,
    ) -> ArtifactCleanup:
        """Delete one bounded batch of expired bytes for terminal deliveries."""
        self._require_limit(limit)
        now = self._aware_now()
        async with self._transactions() as session:
            artifacts = list(
                await session.scalars(
                    select(Artifact)
                    .join(
                        DeliveryIntent,
                        DeliveryIntent.operation_id == Artifact.operation_id,
                    )
                    .where(
                        Artifact.expires_at <= now,
                        DeliveryIntent.state.in_(
                            state.value for state in DeliveryState if state.terminal
                        ),
                    )
                    .order_by(Artifact.expires_at, Artifact.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            candidates = tuple(self._cleanup_candidate(row) for row in artifacts)
            operation_ids = {row.operation_id for row in artifacts}
            if operation_ids:
                await session.execute(
                    update(DeliveryIntent)
                    .where(DeliveryIntent.operation_id.in_(operation_ids))
                    .values(caption=None)
                )
        return await self._purge_artifacts(candidates)

    async def _execute_attempt(
        self,
        operation_id: OperationId,
        started: BeginDeliveryResult,
    ) -> DeliveryOutcome:
        if not isinstance(started, DeliveryAttempt):
            await self._finalize_terminal(operation_id, started)
            return started

        try:
            try:
                loaded_artifacts = []
                for descriptor in started.artifacts:
                    artifact = await self._artifact_store.read(descriptor.key)
                    self._verify_artifact(descriptor, artifact)
                    loaded_artifacts.append(artifact)
                artifacts = tuple(loaded_artifacts)
            except ArtifactStoreError as exc:
                outcome: DeliveryOutcome = DeliveryFailed(type(exc).__name__, False)
            else:
                try:
                    messages = await self._send_media(started, artifacts)
                    outcome = Delivered(
                        tuple(message.message_id for message in messages)
                    )
                except Exception as exc:
                    outcome = classify_delivery_exception(exc)
        except asyncio.CancelledError:
            await self._persist_cancellation_uncertainty(operation_id)
            raise

        try:
            settled = await self._complete_attempt(operation_id, outcome)
        except asyncio.CancelledError:
            await self._persist_cancellation_uncertainty(operation_id)
            raise
        await self._finalize_terminal(operation_id, settled)
        return settled

    async def _begin_attempt(
        self,
        operation_id: OperationId,
    ) -> BeginDeliveryResult:
        async with self._transactions() as session:
            _, intent = await self._locked_state(session, operation_id)
            return await self._start_locked_attempt(
                session,
                operation_id,
                intent,
                authorized_resend=False,
            )

    async def _begin_resend(
        self,
        authorization: ResendAuthorization,
    ) -> tuple[OperationId, BeginDeliveryResult]:
        async with self._transactions() as session:
            (
                operation_id,
                intent,
                actor_user_id,
                stored_target,
            ) = await self._resolve_resend_locked(session, authorization.token)
            if (
                actor_user_id != authorization.actor_user_id
                or stored_target != authorization.target
            ):
                raise DeliveryAuthorizationError("Invalid resend authorization")
            started = await self._start_locked_attempt(
                session,
                operation_id,
                intent,
                authorized_resend=True,
            )
            return operation_id, started

    async def _begin_callback_resend(
        self,
        authorization: ResendCallbackAuthorization,
    ) -> tuple[OperationId, DeliveryTarget, BeginDeliveryResult]:
        async with self._transactions() as session:
            (
                operation_id,
                intent,
                actor_user_id,
                stored_target,
            ) = await self._resolve_resend_locked(session, authorization.token)
            if (
                actor_user_id != authorization.actor_user_id
                or stored_target.chat_id != authorization.chat_id
                or stored_target.thread_id != authorization.thread_id
            ):
                raise DeliveryAuthorizationError("Invalid resend authorization")
            started = await self._start_locked_attempt(
                session,
                operation_id,
                intent,
                authorized_resend=True,
            )
            return operation_id, stored_target, started

    async def _resolve_resend_locked(
        self,
        session: AsyncSession,
        token: str,
    ) -> tuple[OperationId, DeliveryIntent, int, DeliveryTarget]:
        token_hash = self._token_codec.digest(token)
        intent = await session.scalar(
            select(DeliveryIntent)
            .where(DeliveryIntent.resend_token_hash == token_hash)
            .with_for_update()
        )
        if intent is None:
            raise DeliveryAuthorizationError("Invalid resend authorization")
        operation_id = OperationId(intent.operation_id)
        operation = await session.scalar(
            select(PaidOperation)
            .where(PaidOperation.id == operation_id.value)
            .with_for_update()
        )
        if operation is None:
            raise DeliveryAuthorizationError("Invalid resend authorization")
        actor_user_id = await session.scalar(
            select(User.telegram_id)
            .join(
                OperationQuote,
                OperationQuote.requester_id == User.id,
            )
            .where(OperationQuote.id == operation.quote_id)
        )
        if actor_user_id is None:
            raise DeliveryAuthorizationError("Invalid resend authorization")
        return operation_id, intent, actor_user_id, self._target(intent)

    async def _start_locked_attempt(
        self,
        session: AsyncSession,
        operation_id: OperationId,
        intent: DeliveryIntent,
        *,
        authorized_resend: bool,
    ) -> BeginDeliveryResult:
        now = self._aware_now()
        state = DeliveryState(intent.state)
        if state is DeliveryState.DELIVERED:
            return Delivered(tuple(intent.telegram_message_ids))
        if state in {DeliveryState.FAILED, DeliveryState.EXPIRED}:
            return DeliveryFailed(state.value, retryable=False)
        if now >= intent.expires_at:
            intent.state = DeliveryState.EXPIRED.value
            intent.uncertain_at = None
            intent.failed_at = now
            intent.last_error_code = "artifact_expired"
            return DeliveryFailed("artifact_expired", retryable=False)
        if state is DeliveryState.UNCERTAIN and not authorized_resend:
            return DeliveryUncertain("authenticated_resend_required")
        if state is DeliveryState.DELIVERING:
            return DeliveryUncertain("attempt_in_progress_or_interrupted")
        if state is DeliveryState.NOT_READY:
            return DeliveryFailed(
                state.value,
                retryable=True,
            )
        allowed = (
            {DeliveryState.UNCERTAIN} if authorized_resend else {DeliveryState.PENDING}
        )
        if state not in allowed:
            raise DeliveryStateError(f"Cannot deliver intent in {intent.state}")

        rows = list(
            await session.scalars(
                select(Artifact)
                .where(Artifact.operation_id == operation_id.value)
                .order_by(Artifact.ordinal)
            )
        )
        if not rows:
            intent.state = DeliveryState.FAILED.value
            intent.uncertain_at = None
            intent.failed_at = now
            intent.last_error_code = "artifact_missing"
            return DeliveryFailed("artifact_missing", retryable=False)

        intent.state = DeliveryState.DELIVERING.value
        intent.attempt_count += 1
        intent.uncertain_at = None
        intent.failed_at = None
        intent.last_error_code = None
        return DeliveryAttempt(
            operation_id=operation_id,
            target=self._target(intent),
            caption=intent.caption,
            artifacts=tuple(self._artifact_descriptor(row) for row in rows),
        )

    async def _complete_attempt(
        self,
        operation_id: OperationId,
        outcome: DeliveryOutcome,
    ) -> DeliveryOutcome:
        now = self._aware_now()
        async with self._transactions() as session:
            _, intent = await self._locked_state(session, operation_id)
            if intent.state == DeliveryState.DELIVERED.value:
                return Delivered(tuple(intent.telegram_message_ids))
            if intent.state != DeliveryState.DELIVERING.value:
                raise DeliveryStateError(f"Cannot complete delivery in {intent.state}")
            if isinstance(outcome, Delivered):
                intent.state = DeliveryState.DELIVERED.value
                intent.delivered_at = now
                intent.telegram_message_ids = list(outcome.message_ids)
            elif isinstance(outcome, DeliveryUncertain):
                intent.state = DeliveryState.UNCERTAIN.value
                intent.uncertain_at = now
                intent.last_error_code = outcome.code
            elif outcome.retryable:
                intent.state = DeliveryState.PENDING.value
                intent.last_error_code = outcome.code
            else:
                intent.state = DeliveryState.FAILED.value
                intent.failed_at = now
                intent.last_error_code = outcome.code
            return outcome

    async def _persist_cancellation_uncertainty(
        self,
        operation_id: OperationId,
    ) -> None:
        persistence = asyncio.create_task(
            self._complete_attempt(
                operation_id,
                DeliveryUncertain("CancelledError"),
            )
        )
        try:
            await asyncio.shield(persistence)
        except asyncio.CancelledError:
            # A second cancellation must still escape. The shielded transaction
            # continues independently and startup reconciliation is the fallback.
            raise
        except Exception:
            logfire.warning(
                "delivery_cancellation_state_persist_failed",
                operation_id=str(operation_id),
            )

    async def _finalize_terminal(
        self,
        operation_id: OperationId,
        outcome: DeliveryOutcome,
    ) -> None:
        if isinstance(outcome, Delivered):
            await self.cleanup_terminal_artifacts(operation_id)
            return
        if isinstance(outcome, DeliveryFailed) and not outcome.retryable:
            if await self._requires_reversal(operation_id):
                await self._reverse_terminal(operation_id, outcome.code)
            await self.cleanup_terminal_artifacts(operation_id)

    async def _requires_reversal(self, operation_id: OperationId) -> bool:
        async with self._transactions() as session:
            state = await session.scalar(
                select(PaidOperation.state).where(
                    PaidOperation.id == operation_id.value
                )
            )
            if state is None:
                raise DeliveryStateError(f"Unknown operation {operation_id}")
            return state == OperationState.CAPTURED.value

    async def _purge_artifacts(
        self,
        candidates: tuple[_ArtifactCleanupCandidate, ...],
    ) -> ArtifactCleanup:
        if not candidates:
            return ArtifactCleanup(0, 0, 0)
        purgeable_ids: list[uuid.UUID] = []
        failed_count = 0
        for candidate in candidates:
            try:
                await self._artifact_store.delete(candidate.key)
            except ArtifactStoreError:
                failed_count += 1
                logfire.warning(
                    "artifact_cleanup_failed",
                    artifact_key_fingerprint=hashlib.sha256(
                        str(candidate.key).encode()
                    ).hexdigest()[:12],
                )
            else:
                purgeable_ids.append(candidate.artifact_id)

        purged_count = 0
        if purgeable_ids:
            async with self._transactions() as session:
                result = await session.execute(
                    delete(Artifact).where(Artifact.id.in_(purgeable_ids))
                )
                purged_count = max(result.rowcount or 0, 0)
        return ArtifactCleanup(len(candidates), purged_count, failed_count)

    async def _existing_preparation(
        self,
        operation_id: OperationId,
        target: DeliveryTarget,
        caption: str | None,
        media: tuple[DeliveryMedia, ...],
    ) -> PreparedDelivery | None:
        async with self._transactions() as session:
            intent = await session.scalar(
                select(DeliveryIntent).where(
                    DeliveryIntent.operation_id == operation_id.value
                )
            )
            if intent is None:
                return None
            stored_target = self._target(intent)
            if stored_target != target or intent.caption != caption:
                raise DeliveryStateError("Conflicting delivery target for operation")
            rows = tuple(
                await session.scalars(
                    select(Artifact)
                    .where(Artifact.operation_id == operation_id.value)
                    .order_by(Artifact.ordinal)
                )
            )
            if not rows:
                raise DeliveryStateError("Delivery intent has no artifacts")
            expected = tuple(
                (
                    item.artifact_kind.value,
                    item.mime_type,
                    self._filename(item.kind, item.mime_type, ordinal),
                    len(item.data),
                    hashlib.sha256(item.data).hexdigest(),
                )
                for ordinal, item in enumerate(media)
            )
            actual = tuple(
                (
                    row.kind,
                    row.mime_type,
                    row.filename,
                    row.size_bytes,
                    row.sha256,
                )
                for row in rows
            )
            if actual != expected:
                raise DeliveryStateError("Conflicting delivery result for operation")
            resend_token = self._token_codec.issue(intent.id)
            return PreparedDelivery(
                operation_id,
                intent.id,
                resend_token,
                len(rows),
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

    async def _send_media(
        self,
        attempt: DeliveryAttempt,
        artifacts: tuple[StoredArtifact, ...],
    ) -> tuple[Message, ...]:
        batch_kind = classify_delivery_batch(
            tuple(descriptor.kind for descriptor in attempt.artifacts)
        )
        caption = sanitize_for_telegram(attempt.caption) if attempt.caption else None
        common = {
            "chat_id": attempt.target.chat_id,
            "message_thread_id": attempt.target.thread_id,
            "business_connection_id": attempt.target.business_connection_id,
            "reply_to_message_id": attempt.target.reply_to_message_id,
        }
        if batch_kind is DeliveryBatchKind.SINGLE:
            descriptor = attempt.artifacts[0]
            media_file = BufferedInputFile(
                artifacts[0].data,
                filename=descriptor.filename,
            )
            presentation = descriptor.kind
            send_arguments = {
                **common,
                "caption": caption,
                "parse_mode": "HTML" if caption else None,
            }
            if presentation is TelegramMediaKind.PHOTO:
                message = await self._bot.send_photo(
                    **send_arguments,
                    photo=media_file,
                )
            elif presentation is TelegramMediaKind.VIDEO:
                message = await self._bot.send_video(
                    **send_arguments,
                    video=media_file,
                )
            elif presentation is TelegramMediaKind.AUDIO:
                message = await self._bot.send_audio(
                    **send_arguments,
                    audio=media_file,
                )
            elif presentation is TelegramMediaKind.VOICE:
                message = await self._bot.send_voice(
                    **send_arguments,
                    voice=media_file,
                )
            else:
                message = await self._bot.send_document(
                    **send_arguments,
                    document=media_file,
                    disable_content_type_detection=True,
                )
            return (message,)

        media = []
        for index, (descriptor, artifact) in enumerate(
            zip(attempt.artifacts, artifacts, strict=True)
        ):
            item_arguments = {
                "media": BufferedInputFile(
                    artifact.data,
                    filename=descriptor.filename,
                ),
                "caption": caption if index == 0 else None,
                "parse_mode": "HTML" if index == 0 and caption else None,
            }
            if descriptor.kind is TelegramMediaKind.PHOTO:
                item = InputMediaPhoto(**item_arguments)
            elif descriptor.kind is TelegramMediaKind.VIDEO:
                item = InputMediaVideo(**item_arguments)
            elif descriptor.kind is TelegramMediaKind.AUDIO:
                item = InputMediaAudio(**item_arguments)
            else:
                item = InputMediaDocument(
                    **item_arguments,
                    disable_content_type_detection=True,
                )
            media.append(item)
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
        self._require_aware(now, "DeliveryService clock")
        return now

    @staticmethod
    def _inspection(
        intent: DeliveryIntent,
        artifact_count: int,
    ) -> DeliveryInspection:
        return DeliveryInspection(
            operation_id=OperationId(intent.operation_id),
            state=DeliveryState(intent.state),
            target=DeliveryService._target(intent),
            attempt_count=intent.attempt_count,
            artifact_count=artifact_count,
            expires_at=intent.expires_at,
            updated_at=intent.updated_at,
            message_ids=tuple(intent.telegram_message_ids),
            last_error_code=intent.last_error_code,
        )

    @staticmethod
    def _target(intent: DeliveryIntent) -> DeliveryTarget:
        return DeliveryTarget(
            intent.chat_id,
            intent.thread_id,
            intent.reply_to_message_id,
            intent.business_connection_id,
        )

    @staticmethod
    def _artifact_descriptor(row: Artifact) -> ArtifactDescriptor:
        artifact_kind = ArtifactKind(row.kind)
        return ArtifactDescriptor(
            key=ArtifactKey(uuid.UUID(row.storage_key)),
            kind=TelegramMediaKind.from_artifact_kind(artifact_kind),
            mime_type=row.mime_type,
            filename=row.filename,
            size_bytes=row.size_bytes,
            sha256=row.sha256,
        )

    @staticmethod
    def _cleanup_candidate(row: Artifact) -> _ArtifactCleanupCandidate:
        return _ArtifactCleanupCandidate(
            artifact_id=row.id,
            key=ArtifactKey(uuid.UUID(row.storage_key)),
        )

    @staticmethod
    def _require_aware(value: datetime, name: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")

    @staticmethod
    def _require_limit(limit: int) -> None:
        if isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")

    @staticmethod
    def _filename(
        kind: TelegramMediaKind,
        mime_type: str,
        ordinal: int,
    ) -> str:
        extension = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
            "video/mp4": "mp4",
            "audio/mpeg": "mp3",
            "audio/mp3": "mp3",
            "audio/mp4": "m4a",
            "audio/m4a": "m4a",
            "audio/x-m4a": "m4a",
            "audio/ogg": "ogg",
            "application/json": "json",
            "application/pdf": "pdf",
            "application/zip": "zip",
        }.get(mime_type, "bin")
        return f"{kind.artifact_kind.value}_{ordinal + 1}.{extension}"

    @staticmethod
    def _coerce_media(item: MediaContent | DeliveryMedia) -> DeliveryMedia:
        if isinstance(item, DeliveryMedia):
            return item
        if not isinstance(item, MediaContent):
            raise TypeError("delivery media must be MediaContent or DeliveryMedia")
        kind = {
            MediaFamily.IMAGE: TelegramMediaKind.PHOTO,
            MediaFamily.VIDEO: TelegramMediaKind.VIDEO,
            MediaFamily.AUDIO: TelegramMediaKind.AUDIO,
            MediaFamily.DOCUMENT: TelegramMediaKind.DOCUMENT,
        }[item.family]
        return DeliveryMedia(kind, item.mime_type, item.data)

    @staticmethod
    def _verify_artifact(
        descriptor: ArtifactDescriptor,
        artifact: StoredArtifact,
    ) -> None:
        metadata = artifact.metadata
        if (
            metadata.key != descriptor.key
            or metadata.kind is not descriptor.kind.artifact_kind
            or metadata.mime_type != descriptor.mime_type
            or metadata.size_bytes != descriptor.size_bytes
            or metadata.sha256 != descriptor.sha256
            or not descriptor.kind.accepts_mime_type(descriptor.mime_type)
            or descriptor.size_bytes > descriptor.kind.max_size_bytes
            or not _SAFE_FILENAME_PATTERN.fullmatch(descriptor.filename)
        ):
            raise ArtifactIntegrityError(
                "Stored artifact metadata does not match durable delivery metadata"
            )
