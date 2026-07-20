"""Transactional state machine for deferred paid-tool approvals."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pydantic_ai import ModelMessage, ToolCallPart
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from derp.approvals.serialization import (
    HISTORY_SCHEMA_VERSION,
    deserialize_history,
    serialize_deferred_request,
    validate_tool_call_identity,
)
from derp.approvals.tokens import ApprovalTokenCodec
from derp.approvals.types import (
    ApprovalCapability,
    ApprovalChoice,
    ApprovalDecision,
    DecisionDisposition,
    DeferredToolHandle,
    DeferredToolSnapshot,
    DeferredToolStatus,
    ExpirationSweep,
    ResumeClaim,
    ResumeLease,
    ResumeUnavailable,
    ResumeUnavailableReason,
)
from derp.models import (
    Chat,
    DeferredToolRequest,
    OperationQuote,
    PaidOperation,
    User,
)
from derp.operations import OperationId, OperationState, QuoteId

type TransactionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
type LoadedRequest = tuple[DeferredToolRequest, int, int]


class Clock(Protocol):
    def __call__(self) -> datetime: ...


class DeferredToolApprovalError(RuntimeError):
    """Base error for a violated approval-store invariant."""


class ApprovalAuthorizationError(DeferredToolApprovalError):
    """The capability, actor, or Telegram destination did not match."""


class DeferredToolConflictError(DeferredToolApprovalError):
    """An immutable request identity was reused with different state."""


class DeferredToolExpiredError(DeferredToolApprovalError):
    """A new request cannot be created from an expired quote."""


class ApprovalDecisionConflictError(DeferredToolApprovalError):
    """The opposite final decision has already been recorded."""


class ResumeLeaseLostError(DeferredToolApprovalError):
    """A resume worker no longer owns the durable lease marker."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DeferredToolApprovalService:
    """Persist trusted deferred state and own all authenticated transitions."""

    def __init__(
        self,
        transactions: TransactionFactory,
        token_codec: ApprovalTokenCodec,
        *,
        clock: Clock = _utc_now,
        resume_lease_ttl: timedelta = timedelta(minutes=5),
    ) -> None:
        if resume_lease_ttl <= timedelta(0):
            raise ValueError("resume_lease_ttl must be positive")
        self._transactions = transactions
        self._tokens = token_codec
        self._clock = clock
        self._resume_lease_ttl = resume_lease_ttl

    async def create_request(
        self,
        *,
        operation_id: OperationId,
        quote_id: QuoteId,
        message_id: int,
        tool_call: ToolCallPart,
        original_history: list[ModelMessage] | tuple[ModelMessage, ...],
    ) -> DeferredToolHandle:
        """Persist a trusted run once, deriving owner, scope, and TTL from its quote."""
        if not isinstance(operation_id, OperationId):
            raise TypeError("operation_id must be an OperationId")
        if not isinstance(quote_id, QuoteId):
            raise TypeError("quote_id must be a QuoteId")
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or message_id <= 0
        ):
            raise ValueError("message_id must be positive")
        validate_tool_call_identity(tool_call)
        now = self._aware_now()

        async with self._transactions() as session:
            quote_pair = (
                await session.execute(
                    select(OperationQuote, PaidOperation)
                    .join(
                        PaidOperation,
                        PaidOperation.id == OperationQuote.operation_id,
                    )
                    .where(
                        OperationQuote.id == quote_id.value,
                        OperationQuote.operation_id == operation_id.value,
                        PaidOperation.id == operation_id.value,
                    )
                    .with_for_update(of=PaidOperation)
                )
            ).one_or_none()
            if (
                quote_pair is None
                or quote_pair.PaidOperation.quote_id != quote_id.value
            ):
                raise DeferredToolConflictError(
                    "operation and quote do not identify the same paid operation"
                )
            quote = quote_pair.OperationQuote
            operation = quote_pair.PaidOperation

            existing = await self._load_by_identity(
                session,
                operation_id=operation_id.value,
                quote_id=quote_id.value,
            )
            if existing is not None:
                status = DeferredToolStatus(existing[0].status)
                terminal = self._is_terminal(status)
                arguments, history = (
                    (None, None)
                    if terminal
                    else serialize_deferred_request(tool_call, original_history)
                )
                self._assert_immutable_retry(
                    existing[0],
                    quote=quote,
                    message_id=message_id,
                    tool_call=tool_call,
                    arguments=arguments,
                    history=history,
                )
                if terminal:
                    self._scrub_terminal_payload(existing[0], now)
                token = self._tokens.issue(existing[0].id)
                return DeferredToolHandle(
                    snapshot=self._snapshot(existing),
                    callback_token=token,
                    created=False,
                )

            if now >= quote.expires_at:
                raise DeferredToolExpiredError("operation quote has expired")
            if operation.state != OperationState.QUOTED.value:
                raise DeferredToolConflictError(
                    "only a quoted operation can become a deferred request"
                )
            arguments, history = serialize_deferred_request(
                tool_call,
                original_history,
            )

            request_id = uuid.uuid4()
            token = self._tokens.issue(request_id)
            values = {
                "id": request_id,
                "operation_id": operation_id.value,
                "quote_id": quote_id.value,
                "callback_token_hash": self._tokens.digest(token),
                "requester_id": quote.requester_id,
                "chat_id": quote.chat_id,
                "thread_id": quote.thread_id,
                "message_id": message_id,
                "tool_name": tool_call.tool_name,
                "tool_call_id": tool_call.tool_call_id,
                "validated_arguments": deepcopy(arguments),
                "original_history": deepcopy(history),
                "history_schema_version": HISTORY_SCHEMA_VERSION,
                "status": DeferredToolStatus.PENDING.value,
                "expires_at": quote.expires_at,
                "created_at": now,
                "updated_at": now,
            }
            created_id = await session.scalar(
                insert(DeferredToolRequest)
                .values(**values)
                .on_conflict_do_nothing()
                .returning(DeferredToolRequest.id)
            )
            if created_id is None:
                existing = await self._load_by_identity(
                    session,
                    operation_id=operation_id.value,
                    quote_id=quote_id.value,
                )
                if existing is None:
                    raise DeferredToolConflictError(
                        "deferred request identity conflicts with existing state"
                    )
                status = DeferredToolStatus(existing[0].status)
                terminal = self._is_terminal(status)
                self._assert_immutable_retry(
                    existing[0],
                    quote=quote,
                    message_id=message_id,
                    tool_call=tool_call,
                    arguments=None if terminal else arguments,
                    history=None if terminal else history,
                )
                if terminal:
                    self._scrub_terminal_payload(existing[0], now)
                token = self._tokens.issue(existing[0].id)
                return DeferredToolHandle(
                    snapshot=self._snapshot(existing),
                    callback_token=token,
                    created=False,
                )

            loaded = await self._load_by_request_id(session, created_id)
            if loaded is None:  # pragma: no cover - same-transaction insert
                raise DeferredToolApprovalError("created deferred request disappeared")
            return DeferredToolHandle(
                snapshot=self._snapshot(loaded),
                callback_token=token,
                created=True,
            )

    async def inspect(
        self,
        capability: ApprovalCapability,
    ) -> DeferredToolSnapshot:
        """Load content-free state through the same authorization boundary."""
        now = self._aware_now()
        async with self._transactions() as session:
            loaded = await self._load_authorized(session, capability)
            self._expire_if_unclaimed(loaded[0], now)
            self._scrub_terminal_payload(loaded[0], now)
            return self._snapshot(loaded)

    async def approve(
        self,
        capability: ApprovalCapability,
    ) -> ApprovalDecision:
        return await self._decide(capability, ApprovalChoice.APPROVE)

    async def deny(
        self,
        capability: ApprovalCapability,
    ) -> ApprovalDecision:
        return await self._decide(capability, ApprovalChoice.DENY)

    async def claim_resume(
        self,
        capability: ApprovalCapability,
    ) -> ResumeClaim:
        """Atomically claim or recover one bounded approved-run lease."""
        now = self._aware_now()
        async with self._transactions() as session:
            loaded = await self._load_authorized(session, capability)
            request = loaded[0]
            self._expire_if_unclaimed(request, now)
            status = DeferredToolStatus(request.status)
            if status is not DeferredToolStatus.APPROVED:
                self._scrub_terminal_payload(request, now)
                return ResumeUnavailable(
                    reason=self._unavailable_reason(status),
                    snapshot=self._snapshot(loaded),
                )

            if request.resumed_at is not None:
                retry_at = request.resumed_at + self._resume_lease_ttl
                if now < retry_at:
                    return ResumeUnavailable(
                        reason=ResumeUnavailableReason.LEASED,
                        snapshot=self._snapshot(loaded),
                        retry_at=retry_at,
                    )

            arguments, history = self._resume_payload(request)
            request.resumed_at = now
            request.updated_at = now
            return ResumeLease(
                snapshot=self._snapshot(loaded),
                claimed_at=now,
                lease_expires_at=now + self._resume_lease_ttl,
                _validated_arguments=arguments,
                _original_history=history,
            )

    async def renew_resume(self, lease: ResumeLease) -> ResumeLease:
        """Move an owned lease marker forward for provider work exceeding one TTL."""
        if not isinstance(lease, ResumeLease):
            raise TypeError("lease must be a ResumeLease")
        now = self._aware_now()
        async with self._transactions() as session:
            loaded = await self._load_by_request_id(
                session,
                lease.snapshot.request_id,
            )
            if loaded is None:
                raise ResumeLeaseLostError("resume lease no longer exists")
            request = loaded[0]
            if (
                request.status != DeferredToolStatus.APPROVED.value
                or request.resumed_at != lease.claimed_at
            ):
                raise ResumeLeaseLostError("resume lease is no longer owned")
            arguments, history = self._resume_payload(request)
            request.resumed_at = now
            request.updated_at = now
            return ResumeLease(
                snapshot=self._snapshot(loaded),
                claimed_at=now,
                lease_expires_at=now + self._resume_lease_ttl,
                _validated_arguments=arguments,
                _original_history=history,
            )

    async def mark_resumed(self, lease: ResumeLease) -> DeferredToolSnapshot:
        """Complete a lease exactly once after the follow-up run is durable."""
        if not isinstance(lease, ResumeLease):
            raise TypeError("lease must be a ResumeLease")
        now = self._aware_now()
        async with self._transactions() as session:
            loaded = await self._load_by_request_id(
                session,
                lease.snapshot.request_id,
            )
            if loaded is None:
                raise ResumeLeaseLostError("resume lease no longer exists")
            request = loaded[0]
            if request.status == DeferredToolStatus.RESUMED.value:
                self._scrub_terminal_payload(request, now)
                return self._snapshot(loaded)
            if (
                request.status != DeferredToolStatus.APPROVED.value
                or request.resumed_at != lease.claimed_at
            ):
                raise ResumeLeaseLostError("resume lease is no longer owned")
            request.status = DeferredToolStatus.RESUMED.value
            request.resumed_at = now
            request.updated_at = now
            self._scrub_terminal_payload(request, now)
            return self._snapshot(loaded)

    async def release_resume(self, lease: ResumeLease) -> bool:
        """Release an owned claim after a retryable pre-completion failure."""
        if not isinstance(lease, ResumeLease):
            raise TypeError("lease must be a ResumeLease")
        now = self._aware_now()
        async with self._transactions() as session:
            loaded = await self._load_by_request_id(
                session,
                lease.snapshot.request_id,
            )
            if loaded is None:
                return False
            request = loaded[0]
            if (
                request.status != DeferredToolStatus.APPROVED.value
                or request.resumed_at != lease.claimed_at
            ):
                return False
            request.resumed_at = None
            request.updated_at = now
            return True

    async def expire_stale(self, *, limit: int = 100) -> ExpirationSweep:
        """Expire a bounded batch while leaving live resume leases untouched."""
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("limit must be between 1 and 1000")
        now = self._aware_now()
        stale_before = now - self._resume_lease_ttl
        async with self._transactions() as session:
            requests = (
                await session.scalars(
                    select(DeferredToolRequest)
                    .where(
                        DeferredToolRequest.expires_at <= now,
                        or_(
                            DeferredToolRequest.status
                            == DeferredToolStatus.PENDING.value,
                            (
                                DeferredToolRequest.status
                                == DeferredToolStatus.APPROVED.value
                            )
                            & or_(
                                DeferredToolRequest.resumed_at.is_(None),
                                DeferredToolRequest.resumed_at <= stale_before,
                            ),
                        ),
                    )
                    .order_by(DeferredToolRequest.expires_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for request in requests:
                request.status = DeferredToolStatus.EXPIRED.value
                request.decided_at = request.decided_at or now
                request.resumed_at = None
                request.updated_at = now
                self._scrub_terminal_payload(request, now)
            return ExpirationSweep(tuple(request.id for request in requests))

    async def _decide(
        self,
        capability: ApprovalCapability,
        choice: ApprovalChoice,
    ) -> ApprovalDecision:
        now = self._aware_now()
        target = (
            DeferredToolStatus.APPROVED
            if choice is ApprovalChoice.APPROVE
            else DeferredToolStatus.DENIED
        )
        async with self._transactions() as session:
            loaded = await self._load_authorized(session, capability)
            request = loaded[0]
            if self._expire_if_unclaimed(request, now):
                return ApprovalDecision(
                    requested=choice,
                    disposition=DecisionDisposition.EXPIRED,
                    snapshot=self._snapshot(loaded),
                )
            status = DeferredToolStatus(request.status)
            if status is DeferredToolStatus.PENDING:
                request.status = target.value
                request.decided_at = now
                request.updated_at = now
                self._scrub_terminal_payload(request, now)
                return ApprovalDecision(
                    requested=choice,
                    disposition=DecisionDisposition.APPLIED,
                    snapshot=self._snapshot(loaded),
                )
            if status is target or (
                choice is ApprovalChoice.APPROVE
                and status is DeferredToolStatus.RESUMED
            ):
                self._scrub_terminal_payload(request, now)
                return ApprovalDecision(
                    requested=choice,
                    disposition=DecisionDisposition.IDEMPOTENT,
                    snapshot=self._snapshot(loaded),
                )
            if status is DeferredToolStatus.EXPIRED:
                self._scrub_terminal_payload(request, now)
                return ApprovalDecision(
                    requested=choice,
                    disposition=DecisionDisposition.EXPIRED,
                    snapshot=self._snapshot(loaded),
                )
            raise ApprovalDecisionConflictError(
                "deferred tool request already has an incompatible decision"
            )

    async def _load_authorized(
        self,
        session: AsyncSession,
        capability: ApprovalCapability,
    ) -> LoadedRequest:
        if not isinstance(capability, ApprovalCapability):
            raise TypeError("capability must be an ApprovalCapability")
        token_hash = self._tokens.digest(capability.token)
        loaded = (
            await session.execute(
                self._request_query()
                .where(DeferredToolRequest.callback_token_hash == token_hash)
                .with_for_update(of=DeferredToolRequest)
            )
        ).one_or_none()
        if loaded is None:
            raise ApprovalAuthorizationError(
                "approval capability is invalid for this scope"
            )
        request, requester_telegram_id, chat_telegram_id = loaded
        if not (
            self._tokens.verify(request.id, capability.token)
            and requester_telegram_id == capability.requester_telegram_id
            and chat_telegram_id == capability.chat_telegram_id
            and request.thread_id == capability.thread_id
        ):
            raise ApprovalAuthorizationError(
                "approval capability is invalid for this scope"
            )
        return request, requester_telegram_id, chat_telegram_id

    async def _load_by_identity(
        self,
        session: AsyncSession,
        *,
        operation_id: uuid.UUID,
        quote_id: uuid.UUID,
    ) -> LoadedRequest | None:
        rows = (
            await session.execute(
                self._request_query()
                .where(
                    or_(
                        DeferredToolRequest.operation_id == operation_id,
                        DeferredToolRequest.quote_id == quote_id,
                    )
                )
                .with_for_update(of=DeferredToolRequest)
            )
        ).all()
        if not rows:
            return None
        if len(rows) != 1:
            raise DeferredToolConflictError(
                "operation and quote are bound to different deferred requests"
            )
        return rows[0]

    async def _load_by_request_id(
        self,
        session: AsyncSession,
        request_id: uuid.UUID,
    ) -> LoadedRequest | None:
        return (
            await session.execute(
                self._request_query()
                .where(DeferredToolRequest.id == request_id)
                .with_for_update(of=DeferredToolRequest)
            )
        ).one_or_none()

    @staticmethod
    def _request_query():
        return (
            select(DeferredToolRequest, User.telegram_id, Chat.telegram_id)
            .join(User, User.id == DeferredToolRequest.requester_id)
            .join(Chat, Chat.id == DeferredToolRequest.chat_id)
        )

    def _assert_immutable_retry(
        self,
        request: DeferredToolRequest,
        *,
        quote: OperationQuote,
        message_id: int,
        tool_call: ToolCallPart,
        arguments: dict[str, object] | None,
        history: list[dict[str, object]] | None,
    ) -> None:
        expected_token = self._tokens.issue(request.id)
        terminal = self._is_terminal(DeferredToolStatus(request.status))
        payload_matches = terminal or (
            arguments is not None
            and history is not None
            and request.validated_arguments == arguments
            and request.original_history == history
        )
        matches = (
            request.operation_id == quote.operation_id
            and request.quote_id == quote.id
            and request.requester_id == quote.requester_id
            and request.chat_id == quote.chat_id
            and request.thread_id == quote.thread_id
            and request.message_id == message_id
            and request.tool_name == tool_call.tool_name
            and request.tool_call_id == tool_call.tool_call_id
            and payload_matches
            and request.history_schema_version == HISTORY_SCHEMA_VERSION
            and request.expires_at == quote.expires_at
            and request.callback_token_hash == self._tokens.digest(expected_token)
        )
        if not matches:
            raise DeferredToolConflictError(
                "deferred request retry changed immutable trusted state"
            )

    @staticmethod
    def _resume_payload(
        request: DeferredToolRequest,
    ) -> tuple[dict[str, object], tuple[ModelMessage, ...]]:
        history = deserialize_history(
            request.original_history,
            schema_version=request.history_schema_version,
        )
        arguments = deepcopy(request.validated_arguments)
        call = ToolCallPart(
            tool_name=request.tool_name,
            args=arguments,
            tool_call_id=request.tool_call_id,
        )
        normalized_arguments, _ = serialize_deferred_request(call, history)
        if normalized_arguments != arguments:
            raise DeferredToolConflictError(
                "persisted deferred arguments are internally inconsistent"
            )
        return arguments, history

    def _expire_if_unclaimed(
        self,
        request: DeferredToolRequest,
        now: datetime,
    ) -> bool:
        if now < request.expires_at:
            return False
        status = DeferredToolStatus(request.status)
        expirable = status is DeferredToolStatus.PENDING or (
            status is DeferredToolStatus.APPROVED
            and (
                request.resumed_at is None
                or request.resumed_at + self._resume_lease_ttl <= now
            )
        )
        if not expirable:
            return False
        request.status = DeferredToolStatus.EXPIRED.value
        request.decided_at = request.decided_at or now
        request.resumed_at = None
        request.updated_at = now
        self._scrub_terminal_payload(request, now)
        return True

    @staticmethod
    def _is_terminal(status: DeferredToolStatus) -> bool:
        return status in {
            DeferredToolStatus.DENIED,
            DeferredToolStatus.EXPIRED,
            DeferredToolStatus.RESUMED,
        }

    @classmethod
    def _scrub_terminal_payload(
        cls,
        request: DeferredToolRequest,
        now: datetime,
    ) -> None:
        if not cls._is_terminal(DeferredToolStatus(request.status)):
            return
        if request.validated_arguments or request.original_history:
            request.validated_arguments = {}
            request.original_history = []
            request.updated_at = now

    @staticmethod
    def _unavailable_reason(status: DeferredToolStatus) -> ResumeUnavailableReason:
        return {
            DeferredToolStatus.PENDING: ResumeUnavailableReason.PENDING,
            DeferredToolStatus.DENIED: ResumeUnavailableReason.DENIED,
            DeferredToolStatus.EXPIRED: ResumeUnavailableReason.EXPIRED,
            DeferredToolStatus.RESUMED: ResumeUnavailableReason.RESUMED,
        }[status]

    @staticmethod
    def _snapshot(loaded: LoadedRequest) -> DeferredToolSnapshot:
        request, requester_telegram_id, chat_telegram_id = loaded
        return DeferredToolSnapshot(
            request_id=request.id,
            operation_id=OperationId(request.operation_id),
            quote_id=QuoteId(request.quote_id),
            requester_id=request.requester_id,
            requester_telegram_id=requester_telegram_id,
            chat_id=request.chat_id,
            chat_telegram_id=chat_telegram_id,
            thread_id=request.thread_id,
            message_id=request.message_id,
            tool_name=request.tool_name,
            tool_call_id=request.tool_call_id,
            status=DeferredToolStatus(request.status),
            expires_at=request.expires_at,
            decided_at=request.decided_at,
            resumed_at=request.resumed_at,
            created_at=request.created_at,
            updated_at=request.updated_at,
        )

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now


__all__ = [
    "ApprovalAuthorizationError",
    "ApprovalDecisionConflictError",
    "Clock",
    "DeferredToolApprovalError",
    "DeferredToolApprovalService",
    "DeferredToolConflictError",
    "DeferredToolExpiredError",
    "ResumeLeaseLostError",
    "TransactionFactory",
]
