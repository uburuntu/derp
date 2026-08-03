"""Transactional legal acceptance and content-free support services."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.legal import TERMS_ACCEPTANCE_VERSION
from derp.models import (
    LegalAcceptance,
    PaymentReceipt,
    PurchaseIntent,
    SupportIntake,
    SupportRequest,
    User,
)
from derp.support.types import (
    OpenSupportResult,
    OperatorSupportCase,
    OperatorSupportPage,
    ResolveSupportResult,
    SupportCapacityError,
    SupportCase,
    SupportDecisionResult,
    SupportIntakeDraft,
    SupportIntakeLookup,
    SupportIntakeLookupState,
    SupportKind,
    SupportPayment,
    SupportReceiptError,
    SupportSource,
    SupportStatus,
    SupportStatusMessage,
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
    """Open bounded cases and expose exact operator decisions."""

    MAX_OPEN_CASES = 4
    MAX_ACTIVE_INTAKES = 4
    MAX_OPERATOR_CASES = 8
    MAX_MAINTENANCE_BATCH = 100
    CONTENT_RETENTION = timedelta(days=30)
    EXPIRED_INTAKE_RETENTION = timedelta(days=7)

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

    async def register_intake(
        self,
        requester_user_id: uuid.UUID,
        *,
        kind: SupportKind,
        source: SupportSource,
        status_message: SupportStatusMessage,
        payment_receipt_id: uuid.UUID | None = None,
    ) -> SupportIntakeDraft:
        """Bind one short-lived reply prompt to typed case context."""
        if not isinstance(kind, SupportKind) or not isinstance(source, SupportSource):
            raise TypeError("support kind and source must use their enums")
        if not isinstance(status_message, SupportStatusMessage):
            raise TypeError("status_message must be a SupportStatusMessage")
        if payment_receipt_id is not None and not isinstance(
            payment_receipt_id, uuid.UUID
        ):
            raise TypeError("payment_receipt_id must be a UUID")
        now = self._aware_now()
        async with self._transactions() as session:
            user = await session.scalar(
                select(User).where(User.id == requester_user_id).with_for_update()
            )
            if user is None:
                raise LookupError("support requester does not exist")
            active_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(SupportIntake)
                    .where(
                        SupportIntake.requester_user_id == requester_user_id,
                        SupportIntake.expires_at > now,
                    )
                )
                or 0
            )
            if active_count >= self.MAX_ACTIVE_INTAKES:
                raise SupportCapacityError("too many active support prompts")
            if payment_receipt_id is not None:
                receipt = await session.scalar(
                    select(PaymentReceipt.id).where(
                        PaymentReceipt.id == payment_receipt_id,
                        PaymentReceipt.payer_telegram_id == user.telegram_id,
                    )
                )
                if receipt is None:
                    raise SupportReceiptError(
                        "selected payment is unavailable for this requester"
                    )
            intake = SupportIntake(
                requester_user_id=requester_user_id,
                kind=kind.value,
                source=source.value,
                payment_receipt_id=payment_receipt_id,
                prompt_chat_id=status_message.chat_id,
                prompt_message_id=status_message.message_id,
                expires_at=now + timedelta(hours=1),
                created_at=now,
            )
            session.add(intake)
            await session.flush()
        return SupportIntakeDraft(
            kind=kind,
            source=source,
            payment_receipt_id=payment_receipt_id,
            status_message=status_message,
        )

    async def get_intake(
        self,
        requester_user_id: uuid.UUID,
        *,
        prompt_chat_id: int,
        prompt_message_id: int,
    ) -> SupportIntakeDraft | None:
        """Load an unexpired reply prompt for exactly its requester."""
        return (
            await self.lookup_intake(
                requester_user_id,
                prompt_chat_id=prompt_chat_id,
                prompt_message_id=prompt_message_id,
            )
        ).intake

    async def lookup_intake(
        self,
        requester_user_id: uuid.UUID,
        *,
        prompt_chat_id: int,
        prompt_message_id: int,
    ) -> SupportIntakeLookup:
        """Resolve a ForceReply prompt by durable identity, including expiry."""
        if not isinstance(requester_user_id, uuid.UUID):
            raise TypeError("requester_user_id must be a UUID")
        return await self._lookup_intake(
            requester_user_id=requester_user_id,
            prompt_chat_id=prompt_chat_id,
            prompt_message_id=prompt_message_id,
        )

    async def lookup_intake_for_telegram_user(
        self,
        requester_telegram_id: int,
        *,
        prompt_chat_id: int,
        prompt_message_id: int,
    ) -> SupportIntakeLookup:
        """Resolve a prompt before route-scoped database models are injected."""
        if (
            isinstance(requester_telegram_id, bool)
            or not isinstance(requester_telegram_id, int)
            or requester_telegram_id <= 0
        ):
            raise ValueError("requester_telegram_id must be a positive integer")
        return await self._lookup_intake(
            requester_telegram_id=requester_telegram_id,
            prompt_chat_id=prompt_chat_id,
            prompt_message_id=prompt_message_id,
        )

    async def _lookup_intake(
        self,
        *,
        prompt_chat_id: int,
        prompt_message_id: int,
        requester_user_id: uuid.UUID | None = None,
        requester_telegram_id: int | None = None,
    ) -> SupportIntakeLookup:
        """Apply one prompt lookup projection for either trusted requester key."""
        if (requester_user_id is None) == (requester_telegram_id is None):
            raise ValueError("exactly one support requester key is required")
        SupportStatusMessage(
            chat_id=prompt_chat_id,
            message_id=prompt_message_id,
        )
        now = self._aware_now()
        async with self._transactions() as session:
            statement = select(SupportIntake)
            if requester_user_id is not None:
                statement = statement.where(
                    SupportIntake.requester_user_id == requester_user_id
                )
            else:
                statement = statement.join(
                    User,
                    User.id == SupportIntake.requester_user_id,
                ).where(User.telegram_id == requester_telegram_id)
            intake = await session.scalar(
                statement.where(
                    SupportIntake.prompt_chat_id == prompt_chat_id,
                    SupportIntake.prompt_message_id == prompt_message_id,
                )
            )
            if intake is None:
                return SupportIntakeLookup(SupportIntakeLookupState.MISSING)
            if intake.expires_at <= now:
                await session.delete(intake)
                return SupportIntakeLookup(SupportIntakeLookupState.EXPIRED)
            return SupportIntakeLookup(
                SupportIntakeLookupState.FOUND,
                SupportIntakeDraft(
                    kind=SupportKind(intake.kind),
                    source=SupportSource(intake.source),
                    payment_receipt_id=intake.payment_receipt_id,
                    status_message=SupportStatusMessage(
                        chat_id=intake.prompt_chat_id,
                        message_id=intake.prompt_message_id,
                    ),
                ),
            )

    async def discard_intake(
        self,
        requester_user_id: uuid.UUID,
        *,
        prompt_chat_id: int,
        prompt_message_id: int,
    ) -> None:
        """Remove a completed prompt; retry stays safe through case deduplication."""
        async with self._transactions() as session:
            await session.execute(
                delete(SupportIntake).where(
                    SupportIntake.requester_user_id == requester_user_id,
                    SupportIntake.prompt_chat_id == prompt_chat_id,
                    SupportIntake.prompt_message_id == prompt_message_id,
                )
            )

    async def open(
        self,
        requester_user_id: uuid.UUID,
        *,
        kind: SupportKind,
        source: SupportSource,
        description: str | None = None,
        payment_receipt_id: uuid.UUID | None = None,
        status_message: SupportStatusMessage | None = None,
    ) -> OpenSupportResult:
        if not isinstance(kind, SupportKind) or not isinstance(source, SupportSource):
            raise TypeError("support kind and source must use their enums")
        normalized_description = self._description(description)
        if payment_receipt_id is not None and not isinstance(
            payment_receipt_id, uuid.UUID
        ):
            raise TypeError("payment_receipt_id must be a UUID")
        if payment_receipt_id is not None and kind not in {
            SupportKind.PAYMENT,
            SupportKind.REFUND,
        }:
            raise SupportReceiptError("only payment cases can bind a receipt")
        if status_message is not None and not isinstance(
            status_message, SupportStatusMessage
        ):
            raise TypeError("status_message must be a SupportStatusMessage")
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
                    SupportRequest.status.in_(
                        (
                            SupportStatus.OPEN.value,
                            SupportStatus.REFUND_PENDING.value,
                        )
                    ),
                )
            )
            if existing is not None:
                if (
                    self._status_message(existing) is None
                    and status_message is not None
                ):
                    existing.status_message_chat_id = status_message.chat_id
                    existing.status_message_id = status_message.message_id
                    existing.updated_at = now
                    await session.flush()
                return OpenSupportResult(
                    self._case(existing),
                    created=False,
                    status_message=self._status_message(existing),
                )
            open_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(SupportRequest)
                    .where(
                        SupportRequest.requester_user_id == requester_user_id,
                        SupportRequest.status.in_(
                            (
                                SupportStatus.OPEN.value,
                                SupportStatus.REFUND_PENDING.value,
                            )
                        ),
                    )
                )
                or 0
            )
            if open_count >= self.MAX_OPEN_CASES:
                raise SupportCapacityError("too many open support cases")
            if payment_receipt_id is not None:
                payment = await session.scalar(
                    select(PaymentReceipt)
                    .where(
                        PaymentReceipt.id == payment_receipt_id,
                        PaymentReceipt.payer_telegram_id == user.telegram_id,
                    )
                    .with_for_update()
                )
                if payment is None:
                    raise SupportReceiptError(
                        "selected payment is unavailable for this requester"
                    )
            reference = self._reference(self._reference_factory())
            request = SupportRequest(
                reference=reference,
                requester_user_id=requester_user_id,
                kind=kind.value,
                source=source.value,
                description=normalized_description,
                payment_receipt_id=payment_receipt_id,
                status=SupportStatus.OPEN.value,
                status_message_chat_id=status_message and status_message.chat_id,
                status_message_id=status_message and status_message.message_id,
                created_at=now,
                updated_at=now,
            )
            session.add(request)
            await session.flush()
            return OpenSupportResult(
                self._case(request),
                created=True,
                status_message=self._status_message(request),
            )

    async def list_open(self, requester_user_id: uuid.UUID) -> tuple[SupportCase, ...]:
        async with self._transactions() as session:
            requests = tuple(
                await session.scalars(
                    select(SupportRequest)
                    .where(
                        SupportRequest.requester_user_id == requester_user_id,
                        SupportRequest.status.in_(
                            (
                                SupportStatus.OPEN.value,
                                SupportStatus.REFUND_PENDING.value,
                            )
                        ),
                    )
                    .order_by(SupportRequest.created_at, SupportRequest.id)
                    .limit(self.MAX_OPEN_CASES)
                )
            )
        return tuple(self._case(request) for request in requests)

    async def get_requester_case(
        self,
        requester_user_id: uuid.UUID,
        reference: str,
    ) -> SupportCase | None:
        """Load one exact case only for the requester who owns it."""
        if not isinstance(requester_user_id, uuid.UUID):
            raise TypeError("requester_user_id must be a UUID")
        normalized = self._reference(reference)
        async with self._transactions() as session:
            request = await session.scalar(
                select(SupportRequest).where(
                    SupportRequest.requester_user_id == requester_user_id,
                    SupportRequest.reference == normalized,
                )
            )
        return self._case(request) if request is not None else None

    async def list_payments(
        self,
        requester_user_id: uuid.UUID,
        *,
        offset: int = 0,
        limit: int = 5,
    ) -> tuple[SupportPayment, ...]:
        """List the requester's settled receipts without exposing charge IDs."""
        self._page(offset=offset, limit=limit, maximum=10)
        async with self._transactions() as session:
            telegram_id = await session.scalar(
                select(User.telegram_id).where(User.id == requester_user_id)
            )
            if telegram_id is None:
                raise LookupError("support requester does not exist")
            rows = tuple(
                await session.execute(
                    select(PaymentReceipt, PurchaseIntent)
                    .outerjoin(
                        PurchaseIntent,
                        PurchaseIntent.id == PaymentReceipt.purchase_intent_id,
                    )
                    .where(
                        PaymentReceipt.payer_telegram_id == telegram_id,
                        PaymentReceipt.status.in_(
                            ("fulfilled", "refund_requested", "clawed_back")
                        ),
                    )
                    .order_by(
                        PaymentReceipt.created_at.desc(),
                        PaymentReceipt.id.desc(),
                    )
                    .offset(offset)
                    .limit(limit)
                )
            )
        return tuple(self._payment(receipt, intent) for receipt, intent in rows)

    async def list_operator_open(self) -> tuple[OperatorSupportCase, ...]:
        """Return the oldest bounded open cases with operator-only actor IDs."""
        page = await self.list_operator_page()
        return page.cases

    async def list_operator_page(
        self,
        *,
        offset: int = 0,
        limit: int = MAX_OPERATOR_CASES,
    ) -> OperatorSupportPage:
        """Return one deterministic page instead of silently hiding later cases."""
        self._page(offset=offset, limit=limit, maximum=25)
        async with self._transactions() as session:
            active = SupportRequest.status.in_(
                (SupportStatus.OPEN.value, SupportStatus.REFUND_PENDING.value)
            )
            total = int(
                await session.scalar(
                    select(func.count()).select_from(SupportRequest).where(active)
                )
                or 0
            )
            rows = tuple(
                await session.execute(
                    select(
                        SupportRequest,
                        User.telegram_id,
                        User.language_code,
                        PaymentReceipt,
                        PurchaseIntent,
                    )
                    .join(User, User.id == SupportRequest.requester_user_id)
                    .outerjoin(
                        PaymentReceipt,
                        PaymentReceipt.id == SupportRequest.payment_receipt_id,
                    )
                    .outerjoin(
                        PurchaseIntent,
                        PurchaseIntent.id == PaymentReceipt.purchase_intent_id,
                    )
                    .where(active)
                    .order_by(SupportRequest.created_at, SupportRequest.id)
                    .offset(offset)
                    .limit(limit)
                )
            )
        return OperatorSupportPage(
            cases=tuple(
                self._operator_case(
                    request,
                    telegram_id,
                    language_code,
                    receipt,
                    intent,
                )
                for request, telegram_id, language_code, receipt, intent in rows
            ),
            offset=offset,
            total=total,
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
                    select(
                        SupportRequest,
                        User.telegram_id,
                        User.language_code,
                        PaymentReceipt,
                        PurchaseIntent,
                    )
                    .join(User, User.id == SupportRequest.requester_user_id)
                    .outerjoin(
                        PaymentReceipt,
                        PaymentReceipt.id == SupportRequest.payment_receipt_id,
                    )
                    .outerjoin(
                        PurchaseIntent,
                        PurchaseIntent.id == PaymentReceipt.purchase_intent_id,
                    )
                    .where(
                        SupportRequest.reference == normalized,
                        SupportRequest.status.in_(
                            (
                                SupportStatus.OPEN.value,
                                SupportStatus.REFUND_PENDING.value,
                            )
                        ),
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        return self._operator_case(*row)

    async def get_operator_case(
        self,
        reference: str,
    ) -> OperatorSupportCase | None:
        """Load one exact case in any lifecycle state."""
        normalized = self._reference(reference)
        async with self._transactions() as session:
            row = (
                await session.execute(
                    select(
                        SupportRequest,
                        User.telegram_id,
                        User.language_code,
                        PaymentReceipt,
                        PurchaseIntent,
                    )
                    .join(User, User.id == SupportRequest.requester_user_id)
                    .outerjoin(
                        PaymentReceipt,
                        PaymentReceipt.id == SupportRequest.payment_receipt_id,
                    )
                    .outerjoin(
                        PurchaseIntent,
                        PurchaseIntent.id == PaymentReceipt.purchase_intent_id,
                    )
                    .where(SupportRequest.reference == normalized)
                )
            ).one_or_none()
        return self._operator_case(*row) if row is not None else None

    async def decide_operator(
        self,
        reference: str,
        *,
        operator_telegram_id: int,
        status: SupportStatus,
        reason: str,
    ) -> SupportDecisionResult | None:
        """Resolve, decline, or start an exact refund with a visible reason."""
        if status not in {
            SupportStatus.RESOLVED,
            SupportStatus.DECLINED,
            SupportStatus.REFUND_PENDING,
        }:
            raise ValueError("operator decision has an unsupported status")
        if (
            isinstance(operator_telegram_id, bool)
            or not isinstance(operator_telegram_id, int)
            or operator_telegram_id <= 0
        ):
            raise ValueError("operator_telegram_id must be positive")
        normalized_reason = self._description(reason)
        if normalized_reason is None:
            raise ValueError("operator decision reason is required")
        normalized = self._reference(reference)
        async with self._transactions() as session:
            row = (
                await session.execute(
                    select(
                        SupportRequest,
                        User.telegram_id,
                        User.language_code,
                        PaymentReceipt,
                        PurchaseIntent,
                    )
                    .join(User, User.id == SupportRequest.requester_user_id)
                    .outerjoin(
                        PaymentReceipt,
                        PaymentReceipt.id == SupportRequest.payment_receipt_id,
                    )
                    .outerjoin(
                        PurchaseIntent,
                        PurchaseIntent.id == PaymentReceipt.purchase_intent_id,
                    )
                    .where(SupportRequest.reference == normalized)
                    .with_for_update(of=SupportRequest)
                )
            ).one_or_none()
            if row is None:
                return None
            request, telegram_id, language_code, receipt, intent = row
            target = SupportStatus(request.status)
            changed = target is SupportStatus.OPEN
            if changed:
                if status is SupportStatus.REFUND_PENDING and receipt is None:
                    raise SupportReceiptError(
                        "refund decision requires an exact payment"
                    )
                now = self._aware_now()
                request.status = status.value
                request.decision_reason = normalized_reason
                request.decided_at = now
                request.operator_telegram_id = operator_telegram_id
                request.resolved_at = (
                    None if status is SupportStatus.REFUND_PENDING else now
                )
                request.updated_at = now
                await session.flush()
                target = status
            case = self._operator_case(
                request,
                telegram_id,
                language_code,
                receipt,
                intent,
            )
            return SupportDecisionResult(
                case=case,
                status=target,
                reason=request.decision_reason or normalized_reason,
                changed=changed,
            )

    async def resolve_operator(self, reference: str) -> ResolveSupportResult | None:
        """Atomically resolve one case and return its notification target."""
        normalized = self._reference(reference)
        async with self._transactions() as session:
            row = (
                await session.execute(
                    select(
                        SupportRequest,
                        User.telegram_id,
                        User.language_code,
                    )
                    .join(User, User.id == SupportRequest.requester_user_id)
                    .where(SupportRequest.reference == normalized)
                    .with_for_update(of=SupportRequest)
                )
            ).one_or_none()
            if row is None:
                return None
            request, telegram_id, language_code = row
            changed = request.status == SupportStatus.OPEN.value
            if changed:
                now = self._aware_now()
                request.status = SupportStatus.RESOLVED.value
                request.decision_reason = "Resolved by operator."
                request.decided_at = now
                request.resolved_at = now
                request.updated_at = now
                await session.flush()
            resolved_at = request.resolved_at
            if resolved_at is None:
                raise RuntimeError("resolved support case has no resolution time")
            return ResolveSupportResult(
                case=self._operator_case(request, telegram_id, language_code),
                resolved_at=resolved_at,
                changed=changed,
            )

    async def reopen_refund(
        self,
        reference: str,
        *,
        operator_telegram_id: int,
        reason: str,
    ) -> OperatorSupportCase | None:
        """Return a definitively rejected refund case to the actionable queue."""
        if (
            isinstance(operator_telegram_id, bool)
            or not isinstance(operator_telegram_id, int)
            or operator_telegram_id <= 0
        ):
            raise ValueError("operator_telegram_id must be positive")
        normalized_reason = self._description(reason)
        if normalized_reason is None:
            raise ValueError("refund reopening reason is required")
        normalized = self._reference(reference)
        async with self._transactions() as session:
            request = await session.scalar(
                select(SupportRequest)
                .where(SupportRequest.reference == normalized)
                .with_for_update()
            )
            if request is None:
                return None
            if request.status == SupportStatus.REFUND_PENDING.value:
                now = self._aware_now()
                request.status = SupportStatus.OPEN.value
                request.decision_reason = normalized_reason
                request.decided_at = now
                request.operator_telegram_id = operator_telegram_id
                request.resolved_at = None
                request.updated_at = now
                await session.flush()
        return await self.get_operator_case(normalized)

    async def complete_refund(
        self,
        payment_receipt_id: uuid.UUID,
        *,
        reason: str = "Refund complete.",
    ) -> int:
        """Close every pending case bound to one reconciled payment."""
        if not isinstance(payment_receipt_id, uuid.UUID):
            raise TypeError("payment_receipt_id must be a UUID")
        normalized_reason = self._description(reason)
        if normalized_reason is None:
            raise ValueError("refund completion reason is required")
        now = self._aware_now()
        async with self._transactions() as session:
            requests = tuple(
                await session.scalars(
                    select(SupportRequest)
                    .where(
                        SupportRequest.payment_receipt_id == payment_receipt_id,
                        SupportRequest.status == SupportStatus.REFUND_PENDING.value,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for request in requests:
                self._complete_refund_request(request, normalized_reason, now)
            await session.flush()
            return len(requests)

    async def complete_reconciled_refunds(self, *, limit: int = 50) -> int:
        """Close pending cases whose exact receipt is already clawed back."""
        self._page(offset=0, limit=limit, maximum=self.MAX_MAINTENANCE_BATCH)
        now = self._aware_now()
        async with self._transactions() as session:
            requests = tuple(
                await session.scalars(
                    select(SupportRequest)
                    .join(
                        PaymentReceipt,
                        PaymentReceipt.id == SupportRequest.payment_receipt_id,
                    )
                    .where(
                        SupportRequest.status == SupportStatus.REFUND_PENDING.value,
                        PaymentReceipt.status == "clawed_back",
                    )
                    .order_by(SupportRequest.decided_at, SupportRequest.id)
                    .limit(limit)
                    .with_for_update(of=SupportRequest, skip_locked=True)
                )
            )
            for request in requests:
                self._complete_refund_request(request, "Refund complete.", now)
            await session.flush()
            return len(requests)

    async def purge_closed_content(self, *, limit: int = 50) -> int:
        """Purge closed-case free-form text after the fixed retention window."""
        self._page(offset=0, limit=limit, maximum=self.MAX_MAINTENANCE_BATCH)
        now = self._aware_now()
        async with self._transactions() as session:
            requests = tuple(
                await session.scalars(
                    select(SupportRequest)
                    .where(
                        SupportRequest.status.in_(
                            (
                                SupportStatus.RESOLVED.value,
                                SupportStatus.DECLINED.value,
                            )
                        ),
                        SupportRequest.resolved_at <= now - self.CONTENT_RETENTION,
                        SupportRequest.content_purged_at.is_(None),
                    )
                    .order_by(SupportRequest.resolved_at, SupportRequest.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for request in requests:
                request.description = None
                request.decision_reason = None
                request.content_purged_at = now
                request.updated_at = now
            await session.flush()
            return len(requests)

    async def purge_expired_intakes(self, *, limit: int = 50) -> int:
        """Bound stale prompt metadata while retaining short expiry recovery."""
        self._page(offset=0, limit=limit, maximum=self.MAX_MAINTENANCE_BATCH)
        cutoff = self._aware_now() - self.EXPIRED_INTAKE_RETENTION
        async with self._transactions() as session:
            ids = tuple(
                await session.scalars(
                    select(SupportIntake.id)
                    .where(SupportIntake.expires_at <= cutoff)
                    .order_by(SupportIntake.expires_at, SupportIntake.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            if not ids:
                return 0
            result = await session.execute(
                delete(SupportIntake).where(SupportIntake.id.in_(ids))
            )
            return result.rowcount or 0

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("support clock must return an aware datetime")
        return now

    @staticmethod
    def _complete_refund_request(
        request: SupportRequest,
        reason: str,
        now: datetime,
    ) -> None:
        request.status = SupportStatus.RESOLVED.value
        request.decision_reason = reason
        request.resolved_at = now
        request.updated_at = now

    @staticmethod
    def _description(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("support text must be a string")
        normalized = value.strip()
        if not 1 <= len(normalized) <= 800:
            raise ValueError("support text must contain at most 800 characters")
        return normalized

    @staticmethod
    def _page(*, offset: int, limit: int, maximum: int) -> None:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= maximum
        ):
            raise ValueError(f"limit must be between 1 and {maximum}")

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
            description=request.description,
            decision_reason=request.decision_reason,
            status_message=SupportRequestService._status_message(request),
        )

    @staticmethod
    def _operator_case(
        request: SupportRequest,
        telegram_id: int,
        language_code: str | None,
        receipt: PaymentReceipt | None = None,
        intent: PurchaseIntent | None = None,
    ) -> OperatorSupportCase:
        return OperatorSupportCase(
            reference=request.reference,
            kind=SupportKind(request.kind),
            created_at=request.created_at,
            requester_telegram_id=telegram_id,
            description=request.description,
            payment=(
                SupportRequestService._payment(receipt, intent)
                if receipt is not None
                else None
            ),
            status=SupportStatus(request.status),
            status_message=SupportRequestService._status_message(request),
            requester_language_code=language_code or "en",
            decision_reason=request.decision_reason,
        )

    @staticmethod
    def _status_message(request: SupportRequest) -> SupportStatusMessage | None:
        if request.status_message_chat_id is None or request.status_message_id is None:
            return None
        return SupportStatusMessage(
            chat_id=request.status_message_chat_id,
            message_id=request.status_message_id,
        )

    @staticmethod
    def _payment(
        receipt: PaymentReceipt,
        intent: PurchaseIntent | None,
    ) -> SupportPayment:
        return SupportPayment(
            receipt_id=receipt.id,
            stars=receipt.total_amount,
            credits=intent and intent.credits,
            created_at=receipt.created_at,
            status=receipt.status,
        )
