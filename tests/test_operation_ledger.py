"""PostgreSQL contracts for atomic wallet reservation and settlement."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from derp.catalog import GoogleModelKey, ImageResolution
from derp.execution import Feature, plan_execution
from derp.models import (
    Chat,
    OperationQuote,
    PaymentReceipt,
    Subscription,
    SubscriptionCycle,
    User,
    Wallet,
    WalletLot,
)
from derp.operations import (
    DeliveryState,
    FundingAuthorization,
    ImageGenerateQuoteInput,
    ImmutableQuoteConflictError,
    InvalidOperationTransitionError,
    OperationId,
    OperationLedger,
    OperationState,
    Quote,
    QuoteEngine,
    QuoteId,
    ReservationRejected,
    ReservationRejection,
    ReservedOperation,
    WalletOwner,
    WalletOwnerKind,
)

pytestmark = pytest.mark.database


@dataclass(slots=True)
class MutableClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@dataclass(frozen=True, slots=True)
class LedgerEnvironment:
    transactions: Callable[[], AbstractAsyncContextManager[AsyncSession]]
    ledger: OperationLedger
    clock: MutableClock


@pytest_asyncio.fixture
async def ledger_env(db_engine: AsyncEngine) -> AsyncIterator[LedgerEnvironment]:
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    @asynccontextmanager
    async def transactions() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session, session.begin():
            yield session

    clock = MutableClock(datetime(2026, 7, 20, 12, tzinfo=UTC))
    yield LedgerEnvironment(
        transactions=transactions,
        ledger=OperationLedger(transactions, clock=clock),
        clock=clock,
    )


def _telegram_id() -> int:
    return 10_000_000 + uuid4().int % 9_000_000_000


async def _create_scope(
    env: LedgerEnvironment,
    *,
    chat_type: str = "private",
    shared_spending: bool = True,
) -> tuple[UUID, UUID]:
    async with env.transactions() as session:
        user = User(
            telegram_id=_telegram_id(),
            is_bot=False,
            first_name="Ledger",
        )
        chat = Chat(
            telegram_id=_telegram_id(),
            type=chat_type,
            shared_credit_spending_enabled=shared_spending,
        )
        session.add_all([user, chat])
        await session.flush()
        return user.id, chat.id


async def _wallet(
    env: LedgerEnvironment,
    *,
    user_id: UUID | None = None,
    chat_id: UUID | None = None,
) -> Wallet:
    async with env.transactions() as session:
        wallet = Wallet(user_id=user_id, chat_id=chat_id)
        session.add(wallet)
        await session.flush()
        return wallet


async def _add_purchase(env: LedgerEnvironment, wallet_id: UUID, credits: int) -> UUID:
    async with env.transactions() as session:
        lot = WalletLot(
            wallet_id=wallet_id,
            kind="purchased",
            granted_credits=credits,
            available_credits=credits,
        )
        session.add(lot)
        await session.flush()
        return lot.id


async def _add_allowance(
    env: LedgerEnvironment,
    *,
    wallet_id: UUID,
    user_id: UUID,
    credits: int,
    expires_at: datetime,
) -> UUID:
    async with env.transactions() as session:
        receipt = PaymentReceipt(
            telegram_charge_id=f"charge:{uuid4()}",
            provider_charge_id=f"provider:{uuid4()}",
            payer_telegram_id=_telegram_id(),
            currency="XTR",
            total_amount=250,
            payload_token_hash=uuid4().hex,
            is_recurring=True,
            is_first_recurring=True,
            subscription_expiration_at=expires_at,
        )
        subscription = Subscription(
            user_id=user_id,
            plan_id="personal_monthly",
            plan_version="2026-07-20",
            current_period_end=expires_at,
        )
        session.add_all([receipt, subscription])
        await session.flush()
        cycle = SubscriptionCycle(
            subscription_id=subscription.id,
            payment_receipt_id=receipt.id,
            period_start=expires_at - timedelta(days=30),
            period_end=expires_at,
            allowance_credits=credits,
        )
        session.add(cycle)
        await session.flush()
        lot = WalletLot(
            wallet_id=wallet_id,
            kind="allowance",
            subscription_cycle_id=cycle.id,
            granted_credits=credits,
            available_credits=credits,
            expires_at=expires_at,
        )
        session.add(lot)
        await session.flush()
        return lot.id


async def _register_image_operation(
    env: LedgerEnvironment,
    *,
    user_id: UUID,
    chat_id: UUID,
) -> tuple[OperationId, int]:
    operation_id = OperationId(uuid4())
    quote = _image_quote(operation_id, created_at=env.clock())
    await env.ledger.register_quote(
        quote,
        request_key=f"test:{operation_id}",
        requester_id=user_id,
        chat_id=chat_id,
        thread_id=None,
        pricing_input={"input_tokens": 500, "resolution": "1K"},
    )
    return operation_id, quote.credits


def _image_quote(
    operation_id: OperationId,
    *,
    created_at: datetime,
    resolution: ImageResolution = ImageResolution.ONE_K,
) -> Quote:
    return QuoteEngine().quote(
        quote_id=QuoteId.new(),
        operation_id=operation_id,
        plan=plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE),
        quote_input=ImageGenerateQuoteInput(500, resolution),
        created_at=created_at,
    )


async def test_reserve_uses_allowance_then_purchase_and_all_transitions_are_idempotent(
    ledger_env: LedgerEnvironment,
) -> None:
    user_id, chat_id = await _create_scope(ledger_env)
    wallet = await _wallet(ledger_env, user_id=user_id)
    operation_id, price = await _register_image_operation(
        ledger_env, user_id=user_id, chat_id=chat_id
    )
    allowance = max(1, price - 2)
    await _add_allowance(
        ledger_env,
        wallet_id=wallet.id,
        user_id=user_id,
        credits=allowance,
        expires_at=ledger_env.clock() + timedelta(days=10),
    )
    await _add_purchase(ledger_env, wallet.id, 5)

    reserved = await ledger_env.ledger.reserve(operation_id)
    duplicate = await ledger_env.ledger.reserve(operation_id)

    assert isinstance(reserved, ReservedOperation)
    assert reserved.authorization is FundingAuthorization.PRIVATE
    assert reserved.allocation.allowance_credits == allowance
    assert reserved.allocation.purchased_credits == price - allowance
    assert isinstance(duplicate, ReservedOperation) and duplicate.idempotent

    executing = await ledger_env.ledger.mark_executing(operation_id)
    executing_again = await ledger_env.ledger.mark_executing(operation_id)
    captured = await ledger_env.ledger.capture(operation_id)
    captured_again = await ledger_env.ledger.capture(operation_id)
    reversed_ = await ledger_env.ledger.reverse(operation_id, reason="delivery_failed")
    reversed_again = await ledger_env.ledger.reverse(
        operation_id, reason="delivery_failed"
    )

    assert executing.changed and not executing_again.changed
    assert captured.changed and not captured_again.changed
    assert reversed_.changed and not reversed_again.changed
    balance = await ledger_env.ledger.balance(reserved.allocation.owner)
    assert balance.spendable == allowance + 5
    assert balance.reserved == balance.consumed == balance.debt == 0


async def test_group_uses_chat_first_then_requires_explicit_personal_consent(
    ledger_env: LedgerEnvironment,
) -> None:
    user_id, chat_id = await _create_scope(ledger_env, chat_type="supergroup")
    user_wallet = await _wallet(ledger_env, user_id=user_id)
    chat_wallet = await _wallet(ledger_env, chat_id=chat_id)
    first_id, price = await _register_image_operation(
        ledger_env, user_id=user_id, chat_id=chat_id
    )
    await _add_purchase(ledger_env, chat_wallet.id, price)
    await _add_purchase(ledger_env, user_wallet.id, price)

    first = await ledger_env.ledger.reserve(first_id)
    assert isinstance(first, ReservedOperation)
    assert first.allocation.owner.kind is WalletOwnerKind.CHAT
    assert first.authorization is FundingAuthorization.CHAT

    second_id, _ = await _register_image_operation(
        ledger_env, user_id=user_id, chat_id=chat_id
    )
    rejected = await ledger_env.ledger.reserve(second_id)
    assert rejected == ReservationRejected(
        second_id, ReservationRejection.PERSONAL_CONSENT_REQUIRED
    )

    await ledger_env.ledger.grant_personal_consent(user_id, chat_id)
    second = await ledger_env.ledger.reserve(second_id)
    assert isinstance(second, ReservedOperation)
    assert second.allocation.owner.kind is WalletOwnerKind.USER
    assert second.authorization is FundingAuthorization.ALWAYS


async def test_disabled_shared_spending_uses_only_authenticated_one_time_personal_funds(
    ledger_env: LedgerEnvironment,
) -> None:
    user_id, chat_id = await _create_scope(
        ledger_env, chat_type="supergroup", shared_spending=False
    )
    user_wallet = await _wallet(ledger_env, user_id=user_id)
    chat_wallet = await _wallet(ledger_env, chat_id=chat_id)
    operation_id, price = await _register_image_operation(
        ledger_env, user_id=user_id, chat_id=chat_id
    )
    await _add_purchase(ledger_env, chat_wallet.id, price * 2)
    await _add_purchase(ledger_env, user_wallet.id, price)

    rejected = await ledger_env.ledger.reserve(operation_id)
    assert isinstance(rejected, ReservationRejected)
    assert rejected.reason is ReservationRejection.PERSONAL_CONSENT_REQUIRED

    reserved = await ledger_env.ledger.reserve(operation_id, allow_personal_once=True)
    assert isinstance(reserved, ReservedOperation)
    assert reserved.authorization is FundingAuthorization.ONCE
    assert reserved.allocation.owner.kind is WalletOwnerKind.USER


async def test_one_operation_never_splits_between_chat_and_personal_wallets(
    ledger_env: LedgerEnvironment,
) -> None:
    user_id, chat_id = await _create_scope(ledger_env, chat_type="supergroup")
    user_wallet = await _wallet(ledger_env, user_id=user_id)
    chat_wallet = await _wallet(ledger_env, chat_id=chat_id)
    operation_id, price = await _register_image_operation(
        ledger_env, user_id=user_id, chat_id=chat_id
    )
    await _add_purchase(ledger_env, chat_wallet.id, price - 1)
    await _add_purchase(ledger_env, user_wallet.id, price)

    reserved = await ledger_env.ledger.reserve(operation_id, allow_personal_once=True)
    assert isinstance(reserved, ReservedOperation)
    assert reserved.allocation.owner.kind is WalletOwnerKind.USER
    assert reserved.allocation.total_credits == price

    chat_balance = await ledger_env.ledger.balance(
        WalletOwner(WalletOwnerKind.CHAT, chat_id)
    )
    assert chat_balance.spendable == price - 1


async def test_concurrent_exact_balance_reserves_only_one_operation(
    ledger_env: LedgerEnvironment,
) -> None:
    user_id, chat_id = await _create_scope(ledger_env)
    wallet = await _wallet(ledger_env, user_id=user_id)
    first_id, price = await _register_image_operation(
        ledger_env, user_id=user_id, chat_id=chat_id
    )
    second_id, _ = await _register_image_operation(
        ledger_env, user_id=user_id, chat_id=chat_id
    )
    await _add_purchase(ledger_env, wallet.id, price)

    results = await asyncio.gather(
        ledger_env.ledger.reserve(first_id),
        ledger_env.ledger.reserve(second_id),
    )

    assert sum(isinstance(result, ReservedOperation) for result in results) == 1
    rejected = next(
        result for result in results if isinstance(result, ReservationRejected)
    )
    assert rejected.reason is ReservationRejection.INSUFFICIENT_FUNDS


async def test_release_after_allowance_expiry_does_not_restore_spendable_balance(
    ledger_env: LedgerEnvironment,
) -> None:
    user_id, chat_id = await _create_scope(ledger_env)
    wallet = await _wallet(ledger_env, user_id=user_id)
    operation_id, price = await _register_image_operation(
        ledger_env, user_id=user_id, chat_id=chat_id
    )
    expires_at = ledger_env.clock() + timedelta(minutes=5)
    await _add_allowance(
        ledger_env,
        wallet_id=wallet.id,
        user_id=user_id,
        credits=price,
        expires_at=expires_at,
    )
    reserved = await ledger_env.ledger.reserve(operation_id)
    assert isinstance(reserved, ReservedOperation)

    ledger_env.clock.now = expires_at
    released = await ledger_env.ledger.release(operation_id, reason="provider_rejected")
    released_again = await ledger_env.ledger.release(
        operation_id, reason="provider_rejected"
    )

    assert released.changed and not released_again.changed
    balance = await ledger_env.ledger.balance(reserved.allocation.owner)
    assert balance.spendable == balance.reserved == balance.consumed == 0


async def test_register_quote_rejects_conflicting_retry(
    ledger_env: LedgerEnvironment,
) -> None:
    user_id, chat_id = await _create_scope(ledger_env)
    operation_id, _ = await _register_image_operation(
        ledger_env, user_id=user_id, chat_id=chat_id
    )
    async with ledger_env.transactions() as session:
        stored = await session.scalar(
            select(OperationQuote).where(
                OperationQuote.operation_id == operation_id.value
            )
        )
        assert stored is not None

    conflicting = QuoteEngine().quote(
        quote_id=QuoteId.new(),
        operation_id=operation_id,
        plan=plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE),
        quote_input=ImageGenerateQuoteInput(500, ImageResolution.FOUR_K),
        created_at=ledger_env.clock(),
    )
    with pytest.raises(ImmutableQuoteConflictError):
        await ledger_env.ledger.register_quote(
            conflicting,
            request_key=f"test:{operation_id}",
            requester_id=user_id,
            chat_id=chat_id,
            thread_id=None,
            pricing_input={"input_tokens": 500, "resolution": "4K"},
        )


async def test_ensure_quote_returns_original_for_later_clock_and_quote_id(
    ledger_env: LedgerEnvironment,
) -> None:
    user_id, chat_id = await _create_scope(ledger_env)
    operation_id = OperationId(uuid4())
    request_key = f"test:{operation_id}"
    original = _image_quote(operation_id, created_at=ledger_env.clock())
    stored = await ledger_env.ledger.ensure_quote(
        original,
        request_key=request_key,
        requester_id=user_id,
        chat_id=chat_id,
        thread_id=None,
        pricing_input={"input_tokens": 500, "resolution": "1K"},
    )

    ledger_env.clock.now += timedelta(minutes=4)
    retry = _image_quote(operation_id, created_at=ledger_env.clock())
    recovered = await ledger_env.ledger.ensure_quote(
        retry,
        request_key=request_key,
        requester_id=user_id,
        chat_id=chat_id,
        thread_id=None,
        pricing_input={"input_tokens": 500, "resolution": "1K"},
    )

    assert retry.id != original.id
    assert retry.created_at != original.created_at
    assert recovered == stored
    assert recovered.id == original.id
    assert recovered.created_at == original.created_at
    assert recovered.expires_at == original.expires_at


async def test_concurrent_equivalent_quote_registration_returns_one_original(
    ledger_env: LedgerEnvironment,
) -> None:
    user_id, chat_id = await _create_scope(ledger_env)
    operation_id = OperationId(uuid4())
    request_key = f"test:{operation_id}"
    candidates = (
        _image_quote(operation_id, created_at=ledger_env.clock()),
        _image_quote(
            operation_id,
            created_at=ledger_env.clock() + timedelta(seconds=1),
        ),
    )

    results = await asyncio.gather(
        *(
            ledger_env.ledger.ensure_quote(
                quote,
                request_key=request_key,
                requester_id=user_id,
                chat_id=chat_id,
                thread_id=None,
                pricing_input={"input_tokens": 500, "resolution": "1K"},
            )
            for quote in candidates
        )
    )

    assert results[0] == results[1]
    assert results[0].id in {quote.id for quote in candidates}
    assert results[0].created_at in {quote.created_at for quote in candidates}


async def test_ensure_quote_rejects_scope_owner_and_request_identity_conflicts(
    ledger_env: LedgerEnvironment,
) -> None:
    user_id, chat_id = await _create_scope(ledger_env)
    other_user_id, other_chat_id = await _create_scope(ledger_env)
    operation_id = OperationId(uuid4())
    request_key = f"test:{operation_id}"
    original = _image_quote(operation_id, created_at=ledger_env.clock())
    await ledger_env.ledger.ensure_quote(
        original,
        request_key=request_key,
        requester_id=user_id,
        chat_id=chat_id,
        thread_id=None,
        pricing_input={"input_tokens": 500, "resolution": "1K"},
    )
    retry = _image_quote(
        operation_id,
        created_at=ledger_env.clock() + timedelta(seconds=1),
    )

    conflicting_inputs = (
        (f"changed:{operation_id}", user_id, chat_id, retry),
        (request_key, other_user_id, chat_id, retry),
        (request_key, user_id, other_chat_id, retry),
        (
            request_key,
            user_id,
            chat_id,
            _image_quote(
                OperationId(uuid4()),
                created_at=ledger_env.clock() + timedelta(seconds=1),
            ),
        ),
    )
    for (
        conflicting_key,
        conflicting_user,
        conflicting_chat,
        conflicting_quote,
    ) in conflicting_inputs:
        with pytest.raises(ImmutableQuoteConflictError):
            await ledger_env.ledger.ensure_quote(
                conflicting_quote,
                request_key=conflicting_key,
                requester_id=conflicting_user,
                chat_id=conflicting_chat,
                thread_id=None,
                pricing_input={"input_tokens": 500, "resolution": "1K"},
            )


async def test_snapshot_and_execution_claim_prevent_provider_rerun(
    ledger_env: LedgerEnvironment,
) -> None:
    user_id, chat_id = await _create_scope(ledger_env)
    wallet = await _wallet(ledger_env, user_id=user_id)
    operation_id, price = await _register_image_operation(
        ledger_env, user_id=user_id, chat_id=chat_id
    )
    await _add_purchase(ledger_env, wallet.id, price)

    quoted = await ledger_env.ledger.get_snapshot(operation_id)
    assert quoted.state is OperationState.QUOTED
    assert quoted.delivery_state is DeliveryState.NOT_READY
    assert (
        quoted.provider_model_id
        == plan_execution(
            Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE
        ).model.provider_model_id
    )
    assert not quoted.can_claim_execution
    assert not quoted.provider_execution_claimed
    assert quoted.pricing_input == {"input_tokens": 500, "resolution": "1K"}

    reserved = await ledger_env.ledger.reserve(operation_id)
    assert isinstance(reserved, ReservedOperation)
    reserved_snapshot = await ledger_env.ledger.get_snapshot(operation_id)
    assert reserved_snapshot.can_claim_execution
    assert reserved_snapshot.wallet_owner == reserved.allocation.owner

    claims = await asyncio.gather(
        ledger_env.ledger.mark_executing(operation_id),
        ledger_env.ledger.mark_executing(operation_id),
    )
    assert sum(claim.execution_claimed for claim in claims) == 1
    assert sum(claim.changed for claim in claims) == 1

    executing = await ledger_env.ledger.get_snapshot(operation_id)
    assert executing.state is OperationState.EXECUTING
    assert executing.provider_execution_claimed
    assert not executing.can_claim_execution
    with pytest.raises(InvalidOperationTransitionError):
        await ledger_env.ledger.cancel(operation_id, reason="user_canceled")

    released = await ledger_env.ledger.release(
        operation_id, reason="provider_definitively_stopped"
    )
    assert released.state is OperationState.RELEASED
    assert released.changed


async def test_cancel_is_idempotent_before_execution_and_releases_reservation(
    ledger_env: LedgerEnvironment,
) -> None:
    user_id, chat_id = await _create_scope(ledger_env)
    quoted_id, _ = await _register_image_operation(
        ledger_env, user_id=user_id, chat_id=chat_id
    )

    canceled = await ledger_env.ledger.cancel(quoted_id, reason="user_canceled")
    canceled_again = await ledger_env.ledger.cancel(quoted_id, reason="user_canceled")
    assert canceled.state is OperationState.CANCELED and canceled.changed
    assert canceled_again.state is OperationState.CANCELED
    assert not canceled_again.changed

    wallet = await _wallet(ledger_env, user_id=user_id)
    reserved_id, price = await _register_image_operation(
        ledger_env, user_id=user_id, chat_id=chat_id
    )
    await _add_purchase(ledger_env, wallet.id, price)
    reservation = await ledger_env.ledger.reserve(reserved_id)
    assert isinstance(reservation, ReservedOperation)

    released = await ledger_env.ledger.cancel(reserved_id, reason="user_canceled")
    released_again = await ledger_env.ledger.cancel(reserved_id, reason="user_canceled")
    assert released.state is OperationState.RELEASED and released.changed
    assert released_again.state is OperationState.RELEASED
    assert not released_again.changed
    balance = await ledger_env.ledger.balance(reservation.allocation.owner)
    assert balance.spendable == price
    assert balance.reserved == 0
