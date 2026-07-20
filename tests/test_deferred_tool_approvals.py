"""PostgreSQL integration tests for durable deferred paid-tool approvals."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from pydantic_ai import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolApproved,
    ToolCallPart,
    UserPromptPart,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from derp.approvals import (
    ApprovalAuthorizationError,
    ApprovalCapability,
    ApprovalDecisionConflictError,
    ApprovalTokenCodec,
    DecisionDisposition,
    DeferredToolApprovalService,
    DeferredToolConflictError,
    DeferredToolHandle,
    DeferredToolStatus,
    ResumeLease,
    ResumeLeaseLostError,
    ResumeUnavailable,
    ResumeUnavailableReason,
)
from derp.catalog import GoogleModelKey, ImageResolution
from derp.execution import Feature, plan_execution
from derp.models import Chat, DeferredToolRequest, User
from derp.operations import (
    ImageGenerateQuoteInput,
    OperationId,
    OperationLedger,
    Quote,
    QuoteEngine,
    QuoteId,
)

pytestmark = pytest.mark.database


@dataclass(slots=True)
class MutableClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@dataclass(frozen=True, slots=True)
class ApprovalEnvironment:
    transactions: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    ledger: OperationLedger
    service: DeferredToolApprovalService
    clock: MutableClock


@dataclass(frozen=True, slots=True)
class StoredRequest:
    handle: DeferredToolHandle
    capability: ApprovalCapability
    quote: Quote
    operation_id: OperationId
    tool_call: ToolCallPart
    history: tuple[ModelMessage, ...]


@pytest_asyncio.fixture
async def approval_env(db_engine: AsyncEngine) -> AsyncIterator[ApprovalEnvironment]:
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    @asynccontextmanager
    async def transactions() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session, session.begin():
            yield session

    clock = MutableClock(datetime(2026, 7, 20, 12, tzinfo=UTC))
    yield ApprovalEnvironment(
        transactions=transactions,
        ledger=OperationLedger(transactions, clock=clock),
        service=DeferredToolApprovalService(
            transactions,
            ApprovalTokenCodec(b"approval-test-secret".ljust(32, b"!")),
            clock=clock,
            resume_lease_ttl=timedelta(minutes=2),
        ),
        clock=clock,
    )


def _telegram_id() -> int:
    return 10_000_000 + uuid4().int % 9_000_000_000


async def _create_scope(
    env: ApprovalEnvironment,
) -> tuple[UUID, int, UUID, int]:
    user_telegram_id = _telegram_id()
    chat_telegram_id = -_telegram_id()
    async with env.transactions() as session:
        user = User(
            telegram_id=user_telegram_id,
            is_bot=False,
            first_name="Approval",
        )
        chat = Chat(
            telegram_id=chat_telegram_id,
            type="supergroup",
            is_forum=True,
        )
        session.add_all([user, chat])
        await session.flush()
        return user.id, user_telegram_id, chat.id, chat_telegram_id


def _quote(operation_id: OperationId, now: datetime) -> Quote:
    return QuoteEngine().quote(
        quote_id=QuoteId.new(),
        operation_id=operation_id,
        plan=plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE),
        quote_input=ImageGenerateQuoteInput(500, ImageResolution.ONE_K),
        created_at=now,
    )


async def _store_request(
    env: ApprovalEnvironment,
    *,
    thread_id: int | None = 77,
    prompt: str = "private original prompt",
) -> StoredRequest:
    user_id, user_telegram_id, chat_id, chat_telegram_id = await _create_scope(env)
    operation_id = OperationId(uuid4())
    quote = _quote(operation_id, env.clock())
    await env.ledger.register_quote(
        quote,
        request_key=f"approval-test:{operation_id}",
        requester_id=user_id,
        chat_id=chat_id,
        thread_id=thread_id,
        pricing_input={"input_tokens": 500, "resolution": "1K"},
    )
    tool_call = ToolCallPart(
        "generate_image",
        {"prompt": prompt, "resolution": "1K"},
        "tool-call-1",
    )
    history: tuple[ModelMessage, ...] = (
        ModelRequest(parts=[UserPromptPart(prompt)]),
        ModelResponse(parts=[tool_call]),
    )
    handle = await env.service.create_request(
        operation_id=operation_id,
        quote_id=quote.id,
        message_id=1001,
        tool_call=tool_call,
        original_history=history,
    )
    capability = ApprovalCapability(
        token=handle.callback_token,
        requester_telegram_id=user_telegram_id,
        chat_telegram_id=chat_telegram_id,
        thread_id=thread_id,
    )
    return StoredRequest(
        handle=handle,
        capability=capability,
        quote=quote,
        operation_id=operation_id,
        tool_call=tool_call,
        history=history,
    )


async def _stored_payload(
    env: ApprovalEnvironment,
    request_id: UUID,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    async with env.transactions() as session:
        request = await session.scalar(
            select(DeferredToolRequest).where(DeferredToolRequest.id == request_id)
        )
        assert request is not None
        return request.validated_arguments, request.original_history


async def test_create_is_idempotent_only_for_exact_immutable_state(
    approval_env: ApprovalEnvironment,
) -> None:
    stored = await _store_request(approval_env)

    retry = await approval_env.service.create_request(
        operation_id=stored.operation_id,
        quote_id=stored.quote.id,
        message_id=1001,
        tool_call=stored.tool_call,
        original_history=stored.history,
    )

    assert stored.handle.created
    assert not retry.created
    assert retry.snapshot == stored.handle.snapshot
    assert retry.callback_token == stored.handle.callback_token
    assert "private original prompt" not in repr(stored.handle)

    with pytest.raises(DeferredToolConflictError, match="immutable"):
        await approval_env.service.create_request(
            operation_id=stored.operation_id,
            quote_id=stored.quote.id,
            message_id=1002,
            tool_call=stored.tool_call,
            original_history=stored.history,
        )


@pytest.mark.parametrize("mismatch", ["token", "requester", "chat", "thread"])
async def test_decision_authenticates_token_actor_chat_and_topic(
    approval_env: ApprovalEnvironment,
    mismatch: str,
) -> None:
    stored = await _store_request(approval_env)
    values = {
        "token": stored.capability.token,
        "requester_telegram_id": stored.capability.requester_telegram_id,
        "chat_telegram_id": stored.capability.chat_telegram_id,
        "thread_id": stored.capability.thread_id,
    }
    if mismatch == "token":
        replacement = "A" if stored.capability.token[-1] != "A" else "B"
        values["token"] = f"{stored.capability.token[:-1]}{replacement}"
    elif mismatch == "requester":
        values["requester_telegram_id"] += 1
    elif mismatch == "chat":
        values["chat_telegram_id"] -= 1
    else:
        values["thread_id"] = 78

    with pytest.raises(
        ApprovalAuthorizationError,
        match="invalid for this scope",
    ):
        await approval_env.service.approve(ApprovalCapability(**values))


async def test_approve_and_deny_are_idempotent_but_cannot_be_reversed(
    approval_env: ApprovalEnvironment,
) -> None:
    approved = await _store_request(approval_env)

    first = await approval_env.service.approve(approved.capability)
    duplicate = await approval_env.service.approve(approved.capability)

    assert first.disposition is DecisionDisposition.APPLIED
    assert first.snapshot.status is DeferredToolStatus.APPROVED
    assert duplicate.disposition is DecisionDisposition.IDEMPOTENT
    with pytest.raises(ApprovalDecisionConflictError):
        await approval_env.service.deny(approved.capability)

    denied = await _store_request(approval_env)
    first_denial = await approval_env.service.deny(denied.capability)
    duplicate_denial = await approval_env.service.deny(denied.capability)

    assert first_denial.disposition is DecisionDisposition.APPLIED
    assert duplicate_denial.disposition is DecisionDisposition.IDEMPOTENT
    with pytest.raises(ApprovalDecisionConflictError):
        await approval_env.service.approve(denied.capability)

    assert await _stored_payload(
        approval_env,
        denied.handle.snapshot.request_id,
    ) == ({}, [])

    terminal_retry = await approval_env.service.create_request(
        operation_id=denied.operation_id,
        quote_id=denied.quote.id,
        message_id=1001,
        tool_call=ToolCallPart(
            denied.tool_call.tool_name,
            "not-valid-json-and-no-longer-needed",
            denied.tool_call.tool_call_id,
        ),
        original_history=(),
    )
    assert not terminal_retry.created


async def test_concurrent_resume_claims_execute_only_once(
    approval_env: ApprovalEnvironment,
) -> None:
    stored = await _store_request(approval_env)
    await approval_env.service.approve(stored.capability)

    claims = await asyncio.gather(
        approval_env.service.claim_resume(stored.capability),
        approval_env.service.claim_resume(stored.capability),
    )

    leases = [claim for claim in claims if isinstance(claim, ResumeLease)]
    unavailable = [claim for claim in claims if isinstance(claim, ResumeUnavailable)]
    assert len(leases) == 1
    assert len(unavailable) == 1
    assert unavailable[0].reason is ResumeUnavailableReason.LEASED
    run_input = leases[0].build_run_input()
    assert isinstance(
        run_input.deferred_tool_results.approvals["tool-call-1"],
        ToolApproved,
    )
    assert run_input.message_history == stored.history


async def test_stale_resume_claim_is_recovered_and_old_worker_loses_ownership(
    approval_env: ApprovalEnvironment,
) -> None:
    stored = await _store_request(approval_env)
    await approval_env.service.approve(stored.capability)
    old_lease = await approval_env.service.claim_resume(stored.capability)
    assert isinstance(old_lease, ResumeLease)

    approval_env.clock.now += timedelta(minutes=2, seconds=1)
    recovered = await approval_env.service.claim_resume(stored.capability)

    assert isinstance(recovered, ResumeLease)
    assert recovered.claimed_at > old_lease.claimed_at
    with pytest.raises(ResumeLeaseLostError):
        await approval_env.service.mark_resumed(old_lease)
    assert not await approval_env.service.release_resume(old_lease)

    completed = await approval_env.service.mark_resumed(recovered)
    duplicate = await approval_env.service.mark_resumed(recovered)
    after_completion = await approval_env.service.claim_resume(stored.capability)

    assert completed.status is DeferredToolStatus.RESUMED
    assert duplicate.status is DeferredToolStatus.RESUMED
    assert isinstance(after_completion, ResumeUnavailable)
    assert after_completion.reason is ResumeUnavailableReason.RESUMED


async def test_released_or_renewed_lease_preserves_single_owner(
    approval_env: ApprovalEnvironment,
) -> None:
    stored = await _store_request(approval_env)
    await approval_env.service.approve(stored.capability)
    first = await approval_env.service.claim_resume(stored.capability)
    assert isinstance(first, ResumeLease)

    approval_env.clock.now += timedelta(seconds=30)
    renewed = await approval_env.service.renew_resume(first)
    with pytest.raises(ResumeLeaseLostError):
        await approval_env.service.mark_resumed(first)

    assert await approval_env.service.release_resume(renewed)
    replacement = await approval_env.service.claim_resume(stored.capability)
    assert isinstance(replacement, ResumeLease)


async def test_expiry_fails_closed_but_does_not_interrupt_a_live_lease(
    approval_env: ApprovalEnvironment,
) -> None:
    expired = await _store_request(approval_env)
    approval_env.clock.now = expired.quote.expires_at

    decision = await approval_env.service.approve(expired.capability)

    assert decision.disposition is DecisionDisposition.EXPIRED
    assert decision.snapshot.status is DeferredToolStatus.EXPIRED
    assert await _stored_payload(
        approval_env,
        expired.handle.snapshot.request_id,
    ) == ({}, [])
    unavailable = await approval_env.service.claim_resume(expired.capability)
    assert isinstance(unavailable, ResumeUnavailable)
    assert unavailable.reason is ResumeUnavailableReason.EXPIRED

    live = await _store_request(approval_env)
    approval_env.clock.now = live.quote.expires_at - timedelta(seconds=1)
    await approval_env.service.approve(live.capability)
    lease = await approval_env.service.claim_resume(live.capability)
    assert isinstance(lease, ResumeLease)

    approval_env.clock.now += timedelta(seconds=2)
    sweep = await approval_env.service.expire_stale()
    inspection = await approval_env.service.inspect(live.capability)

    assert live.handle.snapshot.request_id not in sweep.request_ids
    assert inspection.status is DeferredToolStatus.APPROVED

    approval_env.clock.now += timedelta(minutes=2)
    sweep = await approval_env.service.expire_stale()

    assert live.handle.snapshot.request_id in sweep.request_ids
    assert (await approval_env.service.inspect(live.capability)).status is (
        DeferredToolStatus.EXPIRED
    )
    assert await _stored_payload(
        approval_env,
        live.handle.snapshot.request_id,
    ) == ({}, [])


async def test_payload_lives_only_until_resume_is_completed(
    approval_env: ApprovalEnvironment,
) -> None:
    sentinel = "resume-sentinel-that-must-be-scrubbed"
    stored = await _store_request(approval_env, prompt=sentinel)
    request_id = stored.handle.snapshot.request_id

    pending_arguments, pending_history = await _stored_payload(
        approval_env,
        request_id,
    )
    assert sentinel in str(pending_arguments)
    assert sentinel in str(pending_history)

    await approval_env.service.approve(stored.capability)
    lease = await approval_env.service.claim_resume(stored.capability)
    assert isinstance(lease, ResumeLease)
    approved_arguments, approved_history = await _stored_payload(
        approval_env,
        request_id,
    )
    assert sentinel in str(approved_arguments)
    assert sentinel in str(approved_history)

    await approval_env.service.mark_resumed(lease)

    assert await _stored_payload(approval_env, request_id) == ({}, [])


async def test_expiration_sweep_scrubs_payload_without_a_callback(
    approval_env: ApprovalEnvironment,
) -> None:
    sentinel = "sweep-sentinel-that-must-be-scrubbed"
    stored = await _store_request(approval_env, prompt=sentinel)
    request_id = stored.handle.snapshot.request_id
    approval_env.clock.now = stored.quote.expires_at

    sweep = await approval_env.service.expire_stale()

    assert request_id in sweep.request_ids
    arguments, history = await _stored_payload(approval_env, request_id)
    assert arguments == {}
    assert history == []
    assert sentinel not in str(arguments)
    assert sentinel not in str(history)
