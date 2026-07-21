"""Contracts for one-shot ordinary chat-turn accounting."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from derp.catalog import GoogleModelKey
from derp.execution import Feature
from derp.features.chat_accounting import (
    ECONOMY_CHAT_PLAN,
    PAID_CHAT_PLAN,
    ChatExecutionAlreadyHandled,
    ChatExecutionInProgress,
    ChatTurnAccounting,
    ChatTurnInvocation,
    EconomyChatExecutionGrant,
    PaidChatExecutionGrant,
)
from derp.models import Chat, User, Wallet, WalletLot
from derp.operations import (
    ChatQuoteInput,
    DeliveryState,
    FundingAuthorization,
    InvalidOperationTransitionError,
    InventoryAllocation,
    OperationLedger,
    OperationSnapshot,
    OperationState,
    Quote,
    QuoteEngine,
    QuoteId,
    ReservationRejected,
    ReservationRejection,
    ReservedOperation,
    SettlementResult,
    WalletOwner,
    WalletOwnerKind,
)

NOW = datetime(2026, 7, 21, 12, tzinfo=UTC)
USER_ID = UUID("79147e88-11d9-47dd-bbed-885d30511e22")
CHAT_ID = UUID("7617a2ba-f30b-4839-a282-b7a845988321")


def _invocation(
    *,
    telegram_chat_id: int = -100_123,
    telegram_message_id: int = 42,
    requester_id: UUID = USER_ID,
    chat_id: UUID = CHAT_ID,
    input_tokens: int = 8_001,
) -> ChatTurnInvocation:
    return ChatTurnInvocation(
        telegram_chat_id=telegram_chat_id,
        telegram_message_id=telegram_message_id,
        requester_id=requester_id,
        chat_id=chat_id,
        thread_id=7,
        estimated_input_tokens=input_tokens,
    )


def _quote(invocation: ChatTurnInvocation) -> Quote:
    return QuoteEngine().quote(
        quote_id=QuoteId.new(),
        operation_id=invocation.operation_id,
        plan=PAID_CHAT_PLAN,
        quote_input=ChatQuoteInput(invocation.estimated_input_tokens),
        created_at=NOW,
    )


def _reservation(
    invocation: ChatTurnInvocation,
    *,
    authorization: FundingAuthorization = FundingAuthorization.PRIVATE,
    idempotent: bool = False,
) -> ReservedOperation:
    owner_kind = (
        WalletOwnerKind.CHAT
        if authorization is FundingAuthorization.CHAT
        else WalletOwnerKind.USER
    )
    owner_id = invocation.chat_id if owner_kind is WalletOwnerKind.CHAT else USER_ID
    return ReservedOperation(
        invocation.operation_id,
        InventoryAllocation(WalletOwner(owner_kind, owner_id), 0, 50),
        authorization,
        idempotent,
    )


def _snapshot(
    invocation: ChatTurnInvocation,
    *,
    state: OperationState,
    authorization: FundingAuthorization | None,
) -> OperationSnapshot:
    quote = _quote(invocation)
    timestamp = NOW if state is not OperationState.QUOTED else None
    return OperationSnapshot(
        operation_id=invocation.operation_id,
        quote=quote,
        provider_model_id=PAID_CHAT_PLAN.model.provider_model_id,
        request_key=invocation.request_key,
        requester_id=invocation.requester_id,
        chat_id=invocation.chat_id,
        thread_id=invocation.thread_id,
        pricing_input={"input_tokens": invocation.estimated_input_tokens},
        state=state,
        delivery_state=DeliveryState.NOT_READY,
        wallet_owner=(
            WalletOwner(WalletOwnerKind.USER, invocation.requester_id)
            if authorization is not None
            else None
        ),
        funding_authorization=authorization,
        result_metadata={},
        terminal_reason=None,
        reserved_at=timestamp,
        execution_started_at=(
            timestamp
            if state
            in {
                OperationState.EXECUTING,
                OperationState.CAPTURED,
                OperationState.RELEASED,
                OperationState.REVERSED,
            }
            else None
        ),
        captured_at=(
            timestamp
            if state in {OperationState.CAPTURED, OperationState.REVERSED}
            else None
        ),
        released_at=timestamp if state is OperationState.RELEASED else None,
        reversed_at=timestamp if state is OperationState.REVERSED else None,
        created_at=NOW,
        updated_at=NOW,
    )


def _mock_ledger() -> MagicMock:
    return MagicMock(spec=OperationLedger)


def test_invocation_derives_stable_content_free_identities() -> None:
    first = _invocation()
    retry = _invocation()
    another = _invocation(telegram_message_id=43)

    assert first.operation_id == retry.operation_id
    assert first.request_key == retry.request_key
    assert first.operation_id != another.operation_id
    assert first.request_key == f"chat-turn:v1:{first.operation_id}"
    assert len(first.request_key) < 255


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"telegram_chat_id": True}, TypeError),
        ({"telegram_chat_id": 0}, ValueError),
        ({"telegram_message_id": 0}, ValueError),
        ({"thread_id": -1}, ValueError),
        ({"estimated_input_tokens": True}, TypeError),
        ({"estimated_input_tokens": -1}, ValueError),
    ],
)
def test_invocation_rejects_ambiguous_identity_or_pricing_inputs(
    changes: dict[str, object],
    error: type[Exception],
) -> None:
    values: dict[str, object] = {
        "telegram_chat_id": -100_123,
        "telegram_message_id": 42,
        "requester_id": USER_ID,
        "chat_id": CHAT_ID,
        "thread_id": 7,
        "estimated_input_tokens": 8_001,
    }
    values.update(changes)

    with pytest.raises(error):
        ChatTurnInvocation(**values)  # type: ignore[arg-type]


async def test_authorize_quotes_exact_standard_band_and_claims_before_grant() -> None:
    invocation = _invocation()
    ledger = _mock_ledger()
    ledger.ensure_quote = AsyncMock(side_effect=lambda quote, **_: quote)
    ledger.reserve = AsyncMock(return_value=_reservation(invocation))
    ledger.mark_executing = AsyncMock(
        return_value=SettlementResult(
            invocation.operation_id,
            OperationState.EXECUTING,
            True,
        )
    )
    accounting = ChatTurnAccounting(ledger, clock=lambda: NOW)
    recorder = MagicMock()
    span = MagicMock()
    span.__enter__.return_value = recorder

    with patch(
        "derp.features.chat_accounting.logfire.span",
        return_value=span,
    ):
        decision = await accounting.authorize(invocation)

    assert isinstance(decision, PaidChatExecutionGrant)
    assert decision.plan is PAID_CHAT_PLAN
    assert decision.quote.key.feature is Feature.CHAT
    assert decision.quote.key.model_key is GoogleModelKey.CHAT_STANDARD
    assert decision.quote.key.context_band.value == "medium"
    ensure_kwargs = ledger.ensure_quote.await_args.kwargs
    assert ensure_kwargs == {
        "request_key": invocation.request_key,
        "requester_id": invocation.requester_id,
        "chat_id": invocation.chat_id,
        "thread_id": invocation.thread_id,
        "pricing_input": {"input_tokens": 8_001},
    }
    ledger.reserve.assert_awaited_once_with(
        invocation.operation_id,
        allow_personal_once=False,
    )
    ledger.mark_executing.assert_awaited_once_with(invocation.operation_id)

    economics = recorder.set_attributes.call_args_list[0].args[0]
    assert economics == {
        "gen_ai.request.model": PAID_CHAT_PLAN.model.provider_model_id,
        "derp.operation.capability": "chat",
        "derp.operation.model_key": "chat_standard",
        "derp.operation.context_band": "medium",
        "derp.operation.quoted_credits": decision.quote.credits,
        "derp.operation.estimated_provider_cost_usd": float(
            decision.quote.estimated_provider_cost_usd
        ),
        "derp.operation.pricing_version": decision.quote.pricing_version,
    }
    decision_attributes = recorder.set_attributes.call_args_list[1].args[0]
    telemetry_keys = set(economics) | set(decision_attributes)
    assert decision_attributes == {
        "derp.operation.authorization": "private",
        "derp.operation.outcome": "paid_execution_granted",
        "derp.operation.terminal_outcome": "none",
    }
    assert not any(
        content_word in key
        for key in telemetry_keys
        for content_word in ("content", "message", "prompt", "query", "argument")
    )


@pytest.mark.parametrize(
    "reason",
    [
        ReservationRejection.PERSONAL_CONSENT_REQUIRED,
        ReservationRejection.INSUFFICIENT_FUNDS,
        ReservationRejection.WALLET_IN_DEBT,
    ],
)
async def test_funding_rejection_cancels_quote_before_one_economy_grant(
    reason: ReservationRejection,
) -> None:
    invocation = _invocation()
    ledger = _mock_ledger()
    ledger.ensure_quote = AsyncMock(side_effect=lambda quote, **_: quote)
    ledger.reserve = AsyncMock(
        return_value=ReservationRejected(invocation.operation_id, reason)
    )
    ledger.cancel = AsyncMock(
        return_value=SettlementResult(
            invocation.operation_id,
            OperationState.CANCELED,
            True,
        )
    )
    ledger.mark_executing = AsyncMock()

    decision = await ChatTurnAccounting(ledger, clock=lambda: NOW).authorize(invocation)

    assert isinstance(decision, EconomyChatExecutionGrant)
    assert decision.plan is ECONOMY_CHAT_PLAN
    assert decision.rejection is reason
    ledger.cancel.assert_awaited_once_with(
        invocation.operation_id,
        reason=f"chat_economy_fallback:{reason.value}",
    )
    ledger.mark_executing.assert_not_awaited()


async def test_duplicate_canceled_fallback_cannot_grant_economy_twice() -> None:
    invocation = _invocation()
    ledger = _mock_ledger()
    ledger.ensure_quote = AsyncMock(side_effect=lambda quote, **_: quote)
    ledger.reserve = AsyncMock(
        return_value=ReservationRejected(
            invocation.operation_id,
            ReservationRejection.PERSONAL_CONSENT_REQUIRED,
        )
    )
    ledger.cancel = AsyncMock(
        return_value=SettlementResult(
            invocation.operation_id,
            OperationState.CANCELED,
            False,
        )
    )
    ledger.get_snapshot = AsyncMock(
        return_value=_snapshot(
            invocation,
            state=OperationState.CANCELED,
            authorization=None,
        )
    )

    decision = await ChatTurnAccounting(ledger, clock=lambda: NOW).authorize(invocation)

    assert isinstance(decision, ChatExecutionAlreadyHandled)
    assert decision.state is OperationState.CANCELED


async def test_existing_captured_operation_never_reclaims_execution() -> None:
    invocation = _invocation()
    ledger = _mock_ledger()
    ledger.ensure_quote = AsyncMock(side_effect=lambda quote, **_: quote)
    ledger.reserve = AsyncMock(return_value=_reservation(invocation, idempotent=True))
    ledger.mark_executing = AsyncMock(
        side_effect=InvalidOperationTransitionError("captured")
    )
    ledger.get_snapshot = AsyncMock(
        return_value=_snapshot(
            invocation,
            state=OperationState.CAPTURED,
            authorization=FundingAuthorization.PRIVATE,
        )
    )

    decision = await ChatTurnAccounting(ledger, clock=lambda: NOW).authorize(invocation)

    assert isinstance(decision, ChatExecutionAlreadyHandled)
    assert decision.state is OperationState.CAPTURED


@pytest.mark.parametrize(
    "state",
    [
        OperationState.CAPTURED,
        OperationState.RELEASED,
        OperationState.REVERSED,
        OperationState.CANCELED,
        OperationState.FAILED,
    ],
)
async def test_terminal_or_captured_retry_is_observed_without_another_claim(
    state: OperationState,
) -> None:
    invocation = _invocation()
    ledger = _mock_ledger()
    ledger.ensure_quote = AsyncMock(side_effect=lambda quote, **_: quote)
    ledger.reserve = AsyncMock(
        return_value=ReservationRejected(
            invocation.operation_id,
            ReservationRejection.OPERATION_TERMINAL,
        )
    )
    ledger.get_snapshot = AsyncMock(
        return_value=_snapshot(
            invocation,
            state=state,
            authorization=(
                None
                if state is OperationState.CANCELED
                else FundingAuthorization.PRIVATE
            ),
        )
    )
    ledger.mark_executing = AsyncMock()

    decision = await ChatTurnAccounting(ledger, clock=lambda: NOW).authorize(invocation)

    assert isinstance(decision, ChatExecutionAlreadyHandled)
    assert decision.state is state
    ledger.mark_executing.assert_not_awaited()


async def test_capture_refuses_an_unclaimed_reserved_operation() -> None:
    invocation = _invocation()
    ledger = _mock_ledger()
    ledger.get_snapshot = AsyncMock(
        return_value=_snapshot(
            invocation,
            state=OperationState.RESERVED,
            authorization=FundingAuthorization.PRIVATE,
        )
    )
    ledger.capture = AsyncMock()

    with pytest.raises(
        InvalidOperationTransitionError,
        match="Cannot capture successful chat execution in reserved",
    ):
        await ChatTurnAccounting(ledger).capture_success(invocation.operation_id)

    ledger.capture.assert_not_awaited()


async def test_settlement_telemetry_records_exact_economics_and_terminal_outcome() -> (
    None
):
    invocation = _invocation()
    snapshot = _snapshot(
        invocation,
        state=OperationState.EXECUTING,
        authorization=FundingAuthorization.PRIVATE,
    )
    ledger = _mock_ledger()
    ledger.get_snapshot = AsyncMock(return_value=snapshot)
    ledger.capture = AsyncMock(
        return_value=SettlementResult(
            invocation.operation_id,
            OperationState.CAPTURED,
            True,
        )
    )
    recorder = MagicMock()
    span = MagicMock()
    span.__enter__.return_value = recorder

    with patch(
        "derp.features.chat_accounting.logfire.span",
        return_value=span,
    ):
        result = await ChatTurnAccounting(ledger).capture_success(
            invocation.operation_id
        )

    assert result.state is OperationState.CAPTURED
    attributes = recorder.set_attributes.call_args_list[1].args[0]
    assert attributes == {
        "derp.operation.authorization": "private",
        "derp.operation.outcome": "settlement",
        "derp.operation.terminal_outcome": "captured",
    }
    assert recorder.set_attributes.call_args_list[0].args[0] == {
        "gen_ai.request.model": PAID_CHAT_PLAN.model.provider_model_id,
        "derp.operation.capability": "chat",
        "derp.operation.model_key": "chat_standard",
        "derp.operation.context_band": "medium",
        "derp.operation.quoted_credits": snapshot.quote.credits,
        "derp.operation.estimated_provider_cost_usd": float(
            snapshot.quote.estimated_provider_cost_usd
        ),
        "derp.operation.pricing_version": snapshot.quote.pricing_version,
    }


@dataclass(slots=True)
class MutableClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@dataclass(frozen=True, slots=True)
class AccountingEnvironment:
    transactions: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    ledger: OperationLedger
    accounting: ChatTurnAccounting
    clock: MutableClock


@pytest_asyncio.fixture
async def accounting_env(
    db_engine: AsyncEngine,
) -> AsyncIterator[AccountingEnvironment]:
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    @asynccontextmanager
    async def transactions() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session, session.begin():
            yield session

    clock = MutableClock(NOW)
    ledger = OperationLedger(transactions, clock=clock)
    yield AccountingEnvironment(
        transactions,
        ledger,
        ChatTurnAccounting(ledger, clock=clock),
        clock,
    )


@dataclass(frozen=True, slots=True)
class Scope:
    user_id: UUID
    chat_id: UUID
    telegram_chat_id: int


def _telegram_id() -> int:
    return 10_000_000 + uuid4().int % 9_000_000_000


async def _create_scope(
    env: AccountingEnvironment,
    *,
    chat_type: str = "private",
    shared_spending: bool = True,
) -> Scope:
    async with env.transactions() as session:
        telegram_chat_id = _telegram_id()
        user = User(
            telegram_id=_telegram_id(),
            is_bot=False,
            first_name="Chat accounting",
        )
        chat = Chat(
            telegram_id=telegram_chat_id,
            type=chat_type,
            shared_credit_spending_enabled=shared_spending,
        )
        session.add_all([user, chat])
        await session.flush()
        return Scope(user.id, chat.id, telegram_chat_id)


async def _fund(
    env: AccountingEnvironment,
    owner: WalletOwner,
    *,
    credits: int = 1_000,
) -> None:
    async with env.transactions() as session:
        wallet = Wallet(
            user_id=owner.id if owner.kind is WalletOwnerKind.USER else None,
            chat_id=owner.id if owner.kind is WalletOwnerKind.CHAT else None,
        )
        session.add(wallet)
        await session.flush()
        session.add(
            WalletLot(
                wallet_id=wallet.id,
                kind="purchased",
                granted_credits=credits,
                available_credits=credits,
            )
        )


def _scope_invocation(scope: Scope, *, message_id: int = 1) -> ChatTurnInvocation:
    return ChatTurnInvocation(
        telegram_chat_id=scope.telegram_chat_id,
        telegram_message_id=message_id,
        requester_id=scope.user_id,
        chat_id=scope.chat_id,
        thread_id=None,
        estimated_input_tokens=5_000,
    )


@pytest.mark.database
async def test_private_turn_is_one_shot_and_capture_and_reversal_are_idempotent(
    accounting_env: AccountingEnvironment,
) -> None:
    scope = await _create_scope(accounting_env)
    owner = WalletOwner(WalletOwnerKind.USER, scope.user_id)
    await _fund(accounting_env, owner)
    invocation = _scope_invocation(scope)

    granted = await accounting_env.accounting.authorize(invocation)
    duplicate = await accounting_env.accounting.authorize(invocation)

    assert isinstance(granted, PaidChatExecutionGrant)
    assert granted.authorization is FundingAuthorization.PRIVATE
    assert isinstance(duplicate, ChatExecutionInProgress)

    captured = await accounting_env.accounting.capture_success(invocation.operation_id)
    captured_again = await accounting_env.accounting.capture_success(
        invocation.operation_id
    )
    after_capture = await accounting_env.accounting.authorize(invocation)
    assert captured.changed and not captured_again.changed
    assert isinstance(after_capture, ChatExecutionAlreadyHandled)
    assert after_capture.state is OperationState.CAPTURED

    reversed_ = await accounting_env.accounting.reverse_delivery_failure(
        invocation.operation_id
    )
    reversed_again = await accounting_env.accounting.reverse_delivery_failure(
        invocation.operation_id
    )
    after_reversal = await accounting_env.accounting.authorize(invocation)
    balance = await accounting_env.ledger.balance(owner)
    assert reversed_.changed and not reversed_again.changed
    assert isinstance(after_reversal, ChatExecutionAlreadyHandled)
    assert after_reversal.state is OperationState.REVERSED
    assert balance.spendable == 1_000
    assert balance.reserved == balance.consumed == 0


@pytest.mark.database
async def test_definitive_provider_failure_releases_once_and_never_reexecutes(
    accounting_env: AccountingEnvironment,
) -> None:
    scope = await _create_scope(accounting_env)
    owner = WalletOwner(WalletOwnerKind.USER, scope.user_id)
    await _fund(accounting_env, owner)
    invocation = _scope_invocation(scope)
    assert isinstance(
        await accounting_env.accounting.authorize(invocation),
        PaidChatExecutionGrant,
    )

    released = await accounting_env.accounting.release_provider_failure(
        invocation.operation_id
    )
    released_again = await accounting_env.accounting.release_provider_failure(
        invocation.operation_id
    )
    retry = await accounting_env.accounting.authorize(invocation)
    balance = await accounting_env.ledger.balance(owner)

    assert released.changed and not released_again.changed
    assert isinstance(retry, ChatExecutionAlreadyHandled)
    assert retry.state is OperationState.RELEASED
    assert balance.spendable == 1_000
    assert balance.reserved == balance.consumed == 0


@pytest.mark.database
async def test_group_spend_is_chat_first_even_when_personal_wallet_can_pay(
    accounting_env: AccountingEnvironment,
) -> None:
    scope = await _create_scope(accounting_env, chat_type="supergroup")
    user_owner = WalletOwner(WalletOwnerKind.USER, scope.user_id)
    chat_owner = WalletOwner(WalletOwnerKind.CHAT, scope.chat_id)
    await _fund(accounting_env, user_owner)
    await _fund(accounting_env, chat_owner)

    decision = await accounting_env.accounting.authorize(_scope_invocation(scope))

    assert isinstance(decision, PaidChatExecutionGrant)
    assert decision.authorization is FundingAuthorization.CHAT
    user_balance = await accounting_env.ledger.balance(user_owner)
    chat_balance = await accounting_env.ledger.balance(chat_owner)
    assert user_balance.spendable == 1_000
    assert chat_balance.reserved == decision.quote.credits


@pytest.mark.database
async def test_group_personal_funds_need_durable_consent_and_never_fall_back_once(
    accounting_env: AccountingEnvironment,
) -> None:
    scope = await _create_scope(accounting_env, chat_type="supergroup")
    user_owner = WalletOwner(WalletOwnerKind.USER, scope.user_id)
    await _fund(accounting_env, user_owner)
    invocation = _scope_invocation(scope)

    fallback = await accounting_env.accounting.authorize(invocation)
    duplicate = await accounting_env.accounting.authorize(invocation)

    assert isinstance(fallback, EconomyChatExecutionGrant)
    assert fallback.rejection is ReservationRejection.PERSONAL_CONSENT_REQUIRED
    assert isinstance(duplicate, ChatExecutionAlreadyHandled)
    assert duplicate.state is OperationState.CANCELED
    balance = await accounting_env.ledger.balance(user_owner)
    assert balance.spendable == 1_000
    assert balance.reserved == 0

    await accounting_env.ledger.grant_personal_consent(
        scope.user_id,
        scope.chat_id,
    )
    consented = await accounting_env.accounting.authorize(
        _scope_invocation(scope, message_id=2)
    )
    assert isinstance(consented, PaidChatExecutionGrant)
    assert consented.authorization is FundingAuthorization.ALWAYS


@pytest.mark.database
async def test_concurrent_duplicate_turn_grants_exactly_one_provider_call(
    accounting_env: AccountingEnvironment,
) -> None:
    scope = await _create_scope(accounting_env)
    await _fund(
        accounting_env,
        WalletOwner(WalletOwnerKind.USER, scope.user_id),
    )
    invocation = _scope_invocation(scope)

    decisions = await asyncio.gather(
        accounting_env.accounting.authorize(invocation),
        accounting_env.accounting.authorize(invocation),
    )

    assert sum(isinstance(item, PaidChatExecutionGrant) for item in decisions) == 1
    assert sum(isinstance(item, ChatExecutionInProgress) for item in decisions) == 1
    snapshot = await accounting_env.ledger.get_snapshot(invocation.operation_id)
    assert snapshot.state is OperationState.EXECUTING


@pytest.mark.database
async def test_private_insufficient_funds_grants_economy_once(
    accounting_env: AccountingEnvironment,
) -> None:
    scope = await _create_scope(accounting_env)
    invocation = _scope_invocation(scope)

    decisions = await asyncio.gather(
        accounting_env.accounting.authorize(invocation),
        accounting_env.accounting.authorize(invocation),
    )
    fallback = next(
        item for item in decisions if isinstance(item, EconomyChatExecutionGrant)
    )
    duplicate = next(
        item for item in decisions if isinstance(item, ChatExecutionAlreadyHandled)
    )

    assert sum(isinstance(item, EconomyChatExecutionGrant) for item in decisions) == 1
    assert fallback.rejection is ReservationRejection.INSUFFICIENT_FUNDS
    assert duplicate.state is OperationState.CANCELED
