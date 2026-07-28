"""Transactional legal acceptance and content-free support services."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.legal import TERMS_ACCEPTANCE_VERSION
from derp.models import LegalAcceptance, SupportRequest, User
from derp.support.types import (
    OpenSupportResult,
    OperatorSupportCase,
    ResolveSupportResult,
    SupportCapacityError,
    SupportCase,
    SupportKind,
    SupportSource,
    SupportStatus,
)

type TransactionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class Clock(Protocol):
    def __call__(self) -> datetime: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TermsAcceptanceService:
    """Record immutable acceptance of the currently published Terms."""

    def __init__(
        self,
        transactions: TransactionFactory,
        *,
        clock: Clock = _utc_now,
    ) -> None:
        self._transactions = transactions
        self._clock = clock

    async def has_current(self, user_id: uuid.UUID) -> bool:
        async with self._transactions() as session:
            return bool(
                await session.scalar(
                    select(
                        select(LegalAcceptance.id)
                        .where(
                            LegalAcceptance.user_id == user_id,
                            LegalAcceptance.document == "terms",
                            LegalAcceptance.version == TERMS_ACCEPTANCE_VERSION,
                        )
                        .exists()
                    )
                )
            )

    async def accept_current(
        self,
        user_id: uuid.UUID,
        *,
        source: str,
    ) -> LegalAcceptance:
        if source not in {"terms_command", "purchase_gate"}:
            raise ValueError("unsupported legal acceptance source")
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("terms acceptance clock must return an aware datetime")
        async with self._transactions() as session:
            user = await session.scalar(
                select(User).where(User.id == user_id).with_for_update()
            )
            if user is None:
                raise LookupError("legal acceptance user does not exist")
            acceptance = await session.scalar(
                select(LegalAcceptance).where(
                    LegalAcceptance.user_id == user_id,
                    LegalAcceptance.document == "terms",
                    LegalAcceptance.version == TERMS_ACCEPTANCE_VERSION,
                )
            )
            if acceptance is not None:
                return acceptance
            acceptance = LegalAcceptance(
                user_id=user_id,
                document="terms",
                version=TERMS_ACCEPTANCE_VERSION,
                source=source,
                accepted_at=now,
            )
            session.add(acceptance)
            await session.flush()
            return acceptance


class SupportRequestService:
    """Open and read a tightly bounded set of content-free support cases."""

    MAX_OPEN_CASES = 4
    MAX_OPERATOR_CASES = 8

    def __init__(
        self,
        transactions: TransactionFactory,
        *,
        clock: Clock = _utc_now,
        reference_factory: Callable[[], str] | None = None,
    ) -> None:
        self._transactions = transactions
        self._clock = clock
        self._reference_factory = reference_factory or (
            lambda: secrets.token_hex(5).upper()
        )

    async def open(
        self,
        requester_user_id: uuid.UUID,
        *,
        kind: SupportKind,
        source: SupportSource,
    ) -> OpenSupportResult:
        if not isinstance(kind, SupportKind) or not isinstance(source, SupportSource):
            raise TypeError("support kind and source must use their enums")
        now = self._aware_now()
        async with self._transactions() as session:
            user = await session.scalar(
                select(User).where(User.id == requester_user_id).with_for_update()
            )
            if user is None:
                raise LookupError("support requester does not exist")
            existing = await session.scalar(
                select(SupportRequest).where(
                    SupportRequest.requester_user_id == requester_user_id,
                    SupportRequest.kind == kind.value,
                    SupportRequest.status == SupportStatus.OPEN.value,
                )
            )
            if existing is not None:
                return OpenSupportResult(self._case(existing), created=False)
            open_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(SupportRequest)
                    .where(
                        SupportRequest.requester_user_id == requester_user_id,
                        SupportRequest.status == SupportStatus.OPEN.value,
                    )
                )
                or 0
            )
            if open_count >= self.MAX_OPEN_CASES:
                raise SupportCapacityError("too many open support cases")
            reference = self._reference(self._reference_factory())
            request = SupportRequest(
                reference=reference,
                requester_user_id=requester_user_id,
                kind=kind.value,
                source=source.value,
                status=SupportStatus.OPEN.value,
                created_at=now,
                updated_at=now,
            )
            session.add(request)
            await session.flush()
            return OpenSupportResult(self._case(request), created=True)

    async def list_open(self, requester_user_id: uuid.UUID) -> tuple[SupportCase, ...]:
        async with self._transactions() as session:
            requests = tuple(
                await session.scalars(
                    select(SupportRequest)
                    .where(
                        SupportRequest.requester_user_id == requester_user_id,
                        SupportRequest.status == SupportStatus.OPEN.value,
                    )
                    .order_by(SupportRequest.created_at, SupportRequest.id)
                    .limit(self.MAX_OPEN_CASES)
                )
            )
        return tuple(self._case(request) for request in requests)

    async def list_operator_open(self) -> tuple[OperatorSupportCase, ...]:
        """Return the oldest bounded open cases with operator-only actor IDs."""
        async with self._transactions() as session:
            rows = tuple(
                await session.execute(
                    select(SupportRequest, User.telegram_id)
                    .join(User, User.id == SupportRequest.requester_user_id)
                    .where(SupportRequest.status == SupportStatus.OPEN.value)
                    .order_by(SupportRequest.created_at, SupportRequest.id)
                    .limit(self.MAX_OPERATOR_CASES)
                )
            )
        return tuple(
            self._operator_case(request, telegram_id) for request, telegram_id in rows
        )

    async def get_operator_open(
        self,
        reference: str,
    ) -> OperatorSupportCase | None:
        """Load one exact open case for a confirmation screen."""
        normalized = self._reference(reference)
        async with self._transactions() as session:
            row = (
                await session.execute(
                    select(SupportRequest, User.telegram_id)
                    .join(User, User.id == SupportRequest.requester_user_id)
                    .where(
                        SupportRequest.reference == normalized,
                        SupportRequest.status == SupportStatus.OPEN.value,
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        return self._operator_case(*row)

    async def resolve_operator(self, reference: str) -> ResolveSupportResult | None:
        """Atomically resolve one case and return its notification target."""
        normalized = self._reference(reference)
        async with self._transactions() as session:
            row = (
                await session.execute(
                    select(SupportRequest, User.telegram_id)
                    .join(User, User.id == SupportRequest.requester_user_id)
                    .where(SupportRequest.reference == normalized)
                    .with_for_update(of=SupportRequest)
                )
            ).one_or_none()
            if row is None:
                return None
            request, telegram_id = row
            changed = request.status == SupportStatus.OPEN.value
            if changed:
                now = self._aware_now()
                request.status = SupportStatus.RESOLVED.value
                request.resolved_at = now
                request.updated_at = now
                await session.flush()
            resolved_at = request.resolved_at
            if resolved_at is None:
                raise RuntimeError("resolved support case has no resolution time")
            return ResolveSupportResult(
                case=self._operator_case(request, telegram_id),
                resolved_at=resolved_at,
                changed=changed,
            )

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("support clock must return an aware datetime")
        return now

    @staticmethod
    def _reference(reference: str) -> str:
        if not isinstance(reference, str):
            raise TypeError("support reference must be a string")
        normalized = reference.strip().upper()
        if (
            not 6 <= len(normalized) <= 16
            or not normalized.isascii()
            or not normalized.isalnum()
        ):
            raise ValueError("support reference must be 6-16 ASCII alphanumeric chars")
        return normalized

    @staticmethod
    def _case(request: SupportRequest) -> SupportCase:
        return SupportCase(
            reference=request.reference,
            kind=SupportKind(request.kind),
            status=SupportStatus(request.status),
            created_at=request.created_at,
        )

    @staticmethod
    def _operator_case(
        request: SupportRequest,
        telegram_id: int,
    ) -> OperatorSupportCase:
        return OperatorSupportCase(
            reference=request.reference,
            kind=SupportKind(request.kind),
            created_at=request.created_at,
            requester_telegram_id=telegram_id,
        )
