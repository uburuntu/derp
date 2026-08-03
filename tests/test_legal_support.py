"""PostgreSQL contracts for versioned Terms and content-free support."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.legal import TERMS_ACCEPTANCE_VERSION
from derp.models import LegalAcceptance, PaymentReceipt, SupportIntake, SupportRequest
from derp.support import (
    SupportCapacityError,
    SupportIntakeLookupState,
    SupportKind,
    SupportRequestService,
    SupportSource,
    SupportStatus,
    SupportStatusMessage,
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


async def test_support_case_keeps_one_bounded_note_and_is_deduplicated(
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
        source=SupportSource.SUPPORT,
        description="Credits did not arrive after payment.",
        status_message=SupportStatusMessage(chat_id=70_002, message_id=21),
    )
    second = await service.open(
        user.id,
        kind=SupportKind.PAYMENT,
        source=SupportSource.SUPPORT,
        description="A duplicate retry must reuse the case.",
        status_message=SupportStatusMessage(chat_id=70_002, message_id=22),
    )

    assert first.created
    assert not second.created
    assert first.case == second.case
    assert first.status_message == SupportStatusMessage(chat_id=70_002, message_id=21)
    assert second.status_message == first.status_message
    assert first.case.description == "Credits did not arrive after payment."
    assert await service.list_open(user.id) == (first.case,)
    assert {column.name for column in SupportRequest.__table__.columns} == {
        "id",
        "reference",
        "requester_user_id",
        "kind",
        "source",
        "description",
        "payment_receipt_id",
        "status",
        "resolved_at",
        "decision_reason",
        "decided_at",
        "operator_telegram_id",
        "status_message_chat_id",
        "status_message_id",
        "content_purged_at",
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


async def test_support_intake_preserves_one_note_and_decline_reason(
    db_session: AsyncSession,
    user_factory,
) -> None:
    user = await user_factory(telegram_id=70_005)

    @asynccontextmanager
    async def transactions():
        yield db_session

    service = SupportRequestService(
        transactions,
        clock=lambda: NOW,
        reference_factory=lambda: "CASE000005",
    )
    status_message = SupportStatusMessage(chat_id=70_005, message_id=51)
    await service.register_intake(
        user.id,
        kind=SupportKind.ACCESS,
        source=SupportSource.SUPPORT,
        status_message=status_message,
    )
    intake = await service.get_intake(
        user.id,
        prompt_chat_id=70_005,
        prompt_message_id=51,
    )

    assert intake is not None
    opened = await service.open(
        user.id,
        kind=intake.kind,
        source=intake.source,
        description="The settings button stopped responding.",
        status_message=intake.status_message,
    )
    await service.discard_intake(
        user.id,
        prompt_chat_id=70_005,
        prompt_message_id=51,
    )

    page = await service.list_operator_page()
    assert page.total == 1
    assert page.cases[0].description == "The settings button stopped responding."
    assert page.cases[0].status_message == status_message

    decision = await service.decide_operator(
        opened.case.reference,
        operator_telegram_id=99,
        status=SupportStatus.DECLINED,
        reason="This account is not restricted; reopen settings and try again.",
    )

    assert decision is not None and decision.changed
    assert decision.status is SupportStatus.DECLINED
    assert decision.reason.startswith("This account")
    assert await service.list_open(user.id) == ()
    assert (
        await service.get_intake(
            user.id,
            prompt_chat_id=70_005,
            prompt_message_id=51,
        )
        is None
    )


async def test_support_intake_lookup_distinguishes_expired_from_missing(
    db_session: AsyncSession,
    user_factory,
) -> None:
    user = await user_factory(telegram_id=70_006)
    current = [NOW]

    @asynccontextmanager
    async def transactions():
        yield db_session

    service = SupportRequestService(transactions, clock=lambda: current[0])
    await service.register_intake(
        user.id,
        kind=SupportKind.ACCESS,
        source=SupportSource.SUPPORT,
        status_message=SupportStatusMessage(chat_id=70_006, message_id=61),
    )

    found = await service.lookup_intake(
        user.id,
        prompt_chat_id=70_006,
        prompt_message_id=61,
    )
    found_by_telegram = await service.lookup_intake_for_telegram_user(
        70_006,
        prompt_chat_id=70_006,
        prompt_message_id=61,
    )
    wrong_actor = await service.lookup_intake_for_telegram_user(
        70_099,
        prompt_chat_id=70_006,
        prompt_message_id=61,
    )
    missing = await service.lookup_intake(
        user.id,
        prompt_chat_id=70_006,
        prompt_message_id=62,
    )
    current[0] += timedelta(hours=2)
    expired = await service.lookup_intake(
        user.id,
        prompt_chat_id=70_006,
        prompt_message_id=61,
    )
    deleted = await service.lookup_intake(
        user.id,
        prompt_chat_id=70_006,
        prompt_message_id=61,
    )

    assert found.state is SupportIntakeLookupState.FOUND
    assert found.intake is not None and found.intake.kind is SupportKind.ACCESS
    assert found_by_telegram == found
    assert wrong_actor.state is SupportIntakeLookupState.MISSING
    assert missing.state is SupportIntakeLookupState.MISSING
    assert expired.state is SupportIntakeLookupState.EXPIRED
    assert deleted.state is SupportIntakeLookupState.MISSING


async def test_support_intake_cap_ignores_expired_prompts_and_purges_after_grace(
    db_session: AsyncSession,
    user_factory,
) -> None:
    user = await user_factory(telegram_id=70_008)
    current = [NOW]

    @asynccontextmanager
    async def transactions():
        yield db_session

    service = SupportRequestService(transactions, clock=lambda: current[0])
    for message_id in range(81, 81 + service.MAX_ACTIVE_INTAKES):
        await service.register_intake(
            user.id,
            kind=SupportKind.ACCESS,
            source=SupportSource.SUPPORT,
            status_message=SupportStatusMessage(
                chat_id=user.telegram_id,
                message_id=message_id,
            ),
        )

    with pytest.raises(SupportCapacityError, match="too many active support prompts"):
        await service.register_intake(
            user.id,
            kind=SupportKind.PRIVACY,
            source=SupportSource.SUPPORT,
            status_message=SupportStatusMessage(
                chat_id=user.telegram_id,
                message_id=99,
            ),
        )

    current[0] += timedelta(hours=1, seconds=1)
    await service.register_intake(
        user.id,
        kind=SupportKind.PRIVACY,
        source=SupportSource.SUPPORT,
        status_message=SupportStatusMessage(
            chat_id=user.telegram_id,
            message_id=99,
        ),
    )
    assert await service.purge_expired_intakes() == 0

    current[0] += service.EXPIRED_INTAKE_RETENTION
    assert await service.purge_expired_intakes() == service.MAX_ACTIVE_INTAKES
    assert await service.purge_expired_intakes() == 0
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(SupportIntake)
            .where(SupportIntake.requester_user_id == user.id)
        )
        == 1
    )


async def test_refund_pending_is_visible_and_can_reopen_then_complete(
    db_session: AsyncSession,
    user_factory,
) -> None:
    user = await user_factory(telegram_id=70_009, language_code="ru")
    other_user = await user_factory(telegram_id=70_010)

    @asynccontextmanager
    async def transactions():
        yield db_session

    receipt = PaymentReceipt(
        telegram_charge_id="support-refund-transition-charge",
        provider_charge_id="support-refund-transition-provider",
        payer_telegram_id=user.telegram_id,
        currency="XTR",
        total_amount=250,
        payload_token_hash="b" * 64,
        status="refund_requested",
    )
    db_session.add(receipt)
    await db_session.flush()
    status_message = SupportStatusMessage(chat_id=user.telegram_id, message_id=91)
    service = SupportRequestService(
        transactions,
        clock=lambda: NOW,
        reference_factory=lambda: "CASE000009",
    )
    opened = await service.open(
        user.id,
        kind=SupportKind.REFUND,
        source=SupportSource.SUPPORT,
        description="Please refund this payment.",
        payment_receipt_id=receipt.id,
        status_message=status_message,
    )

    pending = await service.decide_operator(
        opened.case.reference,
        operator_telegram_id=99,
        status=SupportStatus.REFUND_PENDING,
        reason="Refund requested. Telegram is processing it.",
    )

    assert pending is not None and pending.changed
    assert pending.case.status is SupportStatus.REFUND_PENDING
    assert pending.case.requester_language_code == "ru"
    assert pending.case.status_message == status_message
    assert tuple(case.status for case in await service.list_open(user.id)) == (
        SupportStatus.REFUND_PENDING,
    )
    page = await service.list_operator_page()
    assert page.total == 1
    assert page.cases[0].status is SupportStatus.REFUND_PENDING
    assert (await service.get_operator_open(opened.case.reference)) == page.cases[0]
    requester_case = await service.get_requester_case(
        user.id,
        opened.case.reference,
    )
    assert requester_case is not None
    assert requester_case.status is SupportStatus.REFUND_PENDING
    assert requester_case.decision_reason == pending.reason
    assert requester_case.status_message == status_message
    assert (
        await service.get_requester_case(other_user.id, opened.case.reference) is None
    )

    reopened = await service.reopen_refund(
        opened.case.reference,
        operator_telegram_id=99,
        reason="Telegram rejected the refund. Review the payment and try again.",
    )
    assert reopened is not None
    assert reopened.status is SupportStatus.OPEN
    assert reopened.decision_reason.startswith("Telegram rejected")
    assert (await service.get_operator_open(opened.case.reference)) == reopened

    pending_again = await service.decide_operator(
        opened.case.reference,
        operator_telegram_id=99,
        status=SupportStatus.REFUND_PENDING,
        reason="Refund requested again.",
    )
    assert pending_again is not None and pending_again.changed
    assert await service.complete_refund(receipt.id) == 1
    assert await service.complete_refund(receipt.id) == 0

    completed = await service.get_requester_case(user.id, opened.case.reference)
    assert completed is not None
    assert completed.status is SupportStatus.RESOLVED
    assert completed.decision_reason == "Refund complete."
    assert completed.status_message == status_message
    assert await service.list_open(user.id) == ()
    assert await service.get_operator_open(opened.case.reference) is None


async def test_reconciled_refund_closes_case_and_closed_content_expires(
    db_session: AsyncSession,
    user_factory,
) -> None:
    user = await user_factory(telegram_id=70_007)
    current = [NOW]

    @asynccontextmanager
    async def transactions():
        yield db_session

    receipt = PaymentReceipt(
        telegram_charge_id="support-refund-charge",
        provider_charge_id="support-refund-provider",
        payer_telegram_id=user.telegram_id,
        currency="XTR",
        total_amount=50,
        payload_token_hash="a" * 64,
        status="refund_requested",
    )
    db_session.add(receipt)
    await db_session.flush()
    service = SupportRequestService(
        transactions,
        clock=lambda: current[0],
        reference_factory=lambda: "CASE000007",
    )
    opened = await service.open(
        user.id,
        kind=SupportKind.REFUND,
        source=SupportSource.SUPPORT,
        description="Please refund this payment.",
        payment_receipt_id=receipt.id,
        status_message=SupportStatusMessage(chat_id=user.telegram_id, message_id=71),
    )
    pending = await service.decide_operator(
        opened.case.reference,
        operator_telegram_id=99,
        status=SupportStatus.REFUND_PENDING,
        reason="Refund requested. Telegram is processing it.",
    )
    assert pending is not None and pending.changed
    receipt.status = "clawed_back"
    receipt.clawed_back_at = NOW
    await db_session.flush()

    assert await service.complete_reconciled_refunds() == 1
    row = await db_session.scalar(
        select(SupportRequest).where(SupportRequest.reference == opened.case.reference)
    )
    assert row is not None
    assert row.status == SupportStatus.RESOLVED.value
    assert row.decision_reason == "Refund complete."

    current[0] += timedelta(days=29)
    assert await service.purge_closed_content() == 0
    current[0] += timedelta(days=1)
    assert await service.purge_closed_content() == 1
    await db_session.refresh(row)
    assert row.description is None
    assert row.decision_reason is None
    assert row.content_purged_at == current[0]
    assert row.reference == opened.case.reference
    assert row.status == SupportStatus.RESOLVED.value
    assert row.payment_receipt_id == receipt.id
    assert row.operator_telegram_id == 99
    assert row.status_message_chat_id == user.telegram_id
    assert row.status_message_id == 71
    assert row.resolved_at == NOW
    requester_case = await service.get_requester_case(user.id, opened.case.reference)
    assert requester_case is not None
    assert requester_case.status is SupportStatus.RESOLVED
    assert requester_case.description is None
    assert requester_case.decision_reason is None
    assert requester_case.status_message == SupportStatusMessage(
        chat_id=user.telegram_id,
        message_id=71,
    )
    assert await service.purge_closed_content() == 0
