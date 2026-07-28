"""PostgreSQL contracts for versioned Terms and content-free support."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.legal import TERMS_ACCEPTANCE_VERSION
from derp.models import LegalAcceptance, SupportRequest
from derp.support import (
    SupportKind,
    SupportRequestService,
    SupportSource,
    TermsAcceptanceService,
)

pytestmark = pytest.mark.database
NOW = datetime(2026, 7, 28, 12, tzinfo=UTC)


async def test_terms_acceptance_is_immutable_and_idempotent(
    db_session: AsyncSession,
    user_factory,
) -> None:
    user = await user_factory(telegram_id=70_001)

    @asynccontextmanager
    async def transactions():
        yield db_session

    service = TermsAcceptanceService(transactions, clock=lambda: NOW)

    first = await service.accept_current(user.id, source="terms_command")
    second = await service.accept_current(user.id, source="purchase_gate")

    assert first.id == second.id
    assert first.version == TERMS_ACCEPTANCE_VERSION
    assert await service.has_current(user.id)
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(LegalAcceptance)
            .where(
                LegalAcceptance.user_id == user.id,
                LegalAcceptance.document == "terms",
                LegalAcceptance.version == TERMS_ACCEPTANCE_VERSION,
            )
        )
        == 1
    )


async def test_support_case_is_content_free_bounded_and_deduplicated(
    db_session: AsyncSession,
    user_factory,
) -> None:
    user = await user_factory(telegram_id=70_002)

    @asynccontextmanager
    async def transactions():
        yield db_session

    service = SupportRequestService(
        transactions,
        clock=lambda: NOW,
        reference_factory=lambda: "CASE123456",
    )

    first = await service.open(
        user.id,
        kind=SupportKind.PAYMENT,
        source=SupportSource.PAY_SUPPORT,
    )
    second = await service.open(
        user.id,
        kind=SupportKind.PAYMENT,
        source=SupportSource.SUPPORT,
    )

    assert first.created
    assert not second.created
    assert first.case == second.case
    assert await service.list_open(user.id) == (first.case,)
    assert {column.name for column in SupportRequest.__table__.columns} == {
        "id",
        "reference",
        "requester_user_id",
        "kind",
        "source",
        "status",
        "resolved_at",
        "created_at",
        "updated_at",
    }


async def test_operator_queue_reads_and_resolves_durable_cases(
    db_session: AsyncSession,
    user_factory,
) -> None:
    older_user = await user_factory(telegram_id=70_003)
    newer_user = await user_factory(telegram_id=70_004)
    references = iter(("CASE000003", "CASE000004"))
    times = iter((NOW, NOW + timedelta(minutes=1), NOW + timedelta(minutes=2)))

    @asynccontextmanager
    async def transactions():
        yield db_session

    service = SupportRequestService(
        transactions,
        clock=lambda: next(times),
        reference_factory=lambda: next(references),
    )
    older = await service.open(
        older_user.id,
        kind=SupportKind.PRIVACY,
        source=SupportSource.PRIVACY,
    )
    newer = await service.open(
        newer_user.id,
        kind=SupportKind.ACCESS,
        source=SupportSource.SUPPORT,
    )

    queue = await service.list_operator_open()

    assert [case.reference for case in queue] == [
        older.case.reference,
        newer.case.reference,
    ]
    assert [case.requester_telegram_id for case in queue] == [70_003, 70_004]

    resolved = await service.resolve_operator(older.case.reference.lower())
    repeated = await service.resolve_operator(older.case.reference)

    assert resolved is not None
    assert resolved.changed
    assert resolved.case.requester_telegram_id == 70_003
    assert repeated is not None
    assert not repeated.changed
    assert await service.get_operator_open(older.case.reference) is None
    assert await service.list_open(older_user.id) == ()
    assert tuple(case.reference for case in await service.list_operator_open()) == (
        newer.case.reference,
    )
