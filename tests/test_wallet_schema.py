"""Database-enforced wallet, quote, operation, and ledger invariants."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError

from derp.models import (
    OperationAllocation,
    OperationQuote,
    PaidOperation,
    PaymentReceipt,
    Subscription,
    SubscriptionCycle,
    Wallet,
    WalletLedgerEntry,
    WalletLot,
)

pytestmark = pytest.mark.database


async def _create_wallet(db_session, *, user_id=None, chat_id=None) -> Wallet:
    wallet = Wallet(user_id=user_id, chat_id=chat_id)
    db_session.add(wallet)
    await db_session.flush()
    return wallet


async def _create_allowance_source(db_session, user_id) -> SubscriptionCycle:
    period_start = datetime.now(UTC)
    period_end = period_start + timedelta(days=30)
    receipt = PaymentReceipt(
        telegram_charge_id=f"charge:{uuid4()}",
        provider_charge_id=f"provider:{uuid4()}",
        payer_telegram_id=9_300_001,
        currency="XTR",
        total_amount=250,
        payload_token_hash=uuid4().hex,
        is_recurring=True,
        is_first_recurring=True,
        subscription_expiration_at=period_end,
    )
    subscription = Subscription(
        user_id=user_id,
        plan_id="personal_monthly",
        plan_version="2026-07-20",
        current_period_end=period_end,
    )
    db_session.add_all([receipt, subscription])
    await db_session.flush()

    cycle = SubscriptionCycle(
        subscription_id=subscription.id,
        payment_receipt_id=receipt.id,
        period_start=period_start,
        period_end=period_end,
        allowance_credits=100,
    )
    db_session.add(cycle)
    await db_session.flush()
    return cycle


def _quote(*, operation_id, requester_id, chat_id) -> OperationQuote:
    return OperationQuote(
        operation_id=operation_id,
        request_key=f"request:{uuid4()}",
        requester_id=requester_id,
        chat_id=chat_id,
        feature="chat",
        model_key="chat.standard",
        provider_model_id="gemini-test",
        context_band="small",
        variant="default",
        amount_credits=4,
        estimated_provider_cost_usd=Decimal("0.00100000"),
        pricing_version="2026-07-20",
        catalog_verified_on=date(2026, 7, 20),
        pricing_input={"input_tokens": 1_000},
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )


async def test_personal_and_chat_wallet_inventories_are_valid(
    db_session, user_factory, chat_factory
) -> None:
    user = await user_factory(telegram_id=9_300_002)
    chat = await chat_factory(telegram_id=-9_300_002)
    personal_wallet = await _create_wallet(db_session, user_id=user.id)
    chat_wallet = await _create_wallet(db_session, chat_id=chat.id)
    cycle = await _create_allowance_source(db_session, user.id)

    personal_purchase = WalletLot(
        wallet_id=personal_wallet.id,
        kind="purchased",
        granted_credits=40,
        available_credits=40,
    )
    chat_purchase = WalletLot(
        wallet_id=chat_wallet.id,
        kind="purchased",
        granted_credits=60,
        available_credits=45,
        reserved_credits=5,
        consumed_credits=10,
    )
    allowance = WalletLot(
        wallet_id=personal_wallet.id,
        kind="allowance",
        subscription_cycle_id=cycle.id,
        granted_credits=100,
        available_credits=80,
        consumed_credits=20,
        expires_at=cycle.period_end,
    )
    db_session.add_all([personal_purchase, chat_purchase, allowance])
    await db_session.flush()

    assert personal_purchase.available_credits == 40
    assert chat_purchase.reserved_credits == 5
    assert allowance.expires_at == cycle.period_end


@pytest.mark.parametrize(
    ("has_user", "has_chat"),
    [(False, False), (True, True)],
    ids=["owner-missing", "two-owners"],
)
async def test_wallet_requires_exactly_one_owner(
    db_session, user_factory, chat_factory, has_user, has_chat
) -> None:
    user = await user_factory(telegram_id=9_300_003)
    chat = await chat_factory(telegram_id=-9_300_003)
    db_session.add(
        Wallet(
            user_id=user.id if has_user else None,
            chat_id=chat.id if has_chat else None,
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize("owner_kind", ["user", "chat"])
async def test_wallet_owner_is_unique(
    db_session, user_factory, chat_factory, owner_kind
) -> None:
    user = await user_factory(telegram_id=9_300_004)
    chat = await chat_factory(telegram_id=-9_300_004)
    owner = {f"{owner_kind}_id": user.id if owner_kind == "user" else chat.id}
    await _create_wallet(db_session, **owner)
    db_session.add(Wallet(**owner))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_wallet_debt_cannot_be_negative(db_session, user_factory) -> None:
    user = await user_factory(telegram_id=9_300_005)
    db_session.add(Wallet(user_id=user.id, debt_credits=-1))

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize(
    ("granted", "available", "consumed"),
    [(1, -1, 2), (10, 9, 0)],
    ids=["negative-counter", "unreconciled-counters"],
)
async def test_wallet_lot_counters_are_nonnegative_and_reconciled(
    db_session, user_factory, granted, available, consumed
) -> None:
    user = await user_factory(telegram_id=9_300_006)
    wallet = await _create_wallet(db_session, user_id=user.id)
    db_session.add(
        WalletLot(
            wallet_id=wallet.id,
            kind="purchased",
            granted_credits=granted,
            available_credits=available,
            consumed_credits=consumed,
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize(
    ("kind", "include_source", "include_expiry"),
    [
        ("allowance", False, True),
        ("allowance", True, False),
        ("purchased", True, True),
    ],
    ids=[
        "allowance-without-cycle",
        "allowance-without-expiry",
        "purchase-with-allowance-source",
    ],
)
async def test_wallet_lot_kind_requires_matching_source_and_expiry(
    db_session,
    user_factory,
    kind,
    include_source,
    include_expiry,
) -> None:
    user = await user_factory(telegram_id=9_300_007)
    wallet = await _create_wallet(db_session, user_id=user.id)
    cycle = await _create_allowance_source(db_session, user.id)
    db_session.add(
        WalletLot(
            wallet_id=wallet.id,
            kind=kind,
            subscription_cycle_id=cycle.id if include_source else None,
            granted_credits=10,
            available_credits=10,
            expires_at=cycle.period_end if include_expiry else None,
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_quote_and_operation_form_a_one_to_one_fk_shape(
    db_session, user_factory, chat_factory
) -> None:
    user = await user_factory(telegram_id=9_300_008)
    chat = await chat_factory(telegram_id=-9_300_008)
    wallet = await _create_wallet(db_session, user_id=user.id)
    operation_id = uuid4()
    quote = _quote(
        operation_id=operation_id,
        requester_id=user.id,
        chat_id=chat.id,
    )
    db_session.add(quote)
    await db_session.flush()

    operation = PaidOperation(
        id=operation_id,
        quote_id=quote.id,
        wallet_id=wallet.id,
        funding_authorization="private",
    )
    db_session.add(operation)
    await db_session.flush()

    assert operation.id == quote.operation_id
    assert operation.quote_id == quote.id
    assert operation.wallet_id == wallet.id


async def test_operation_id_must_match_immutable_quote_identity(
    db_session, user_factory, chat_factory
) -> None:
    user = await user_factory(telegram_id=9_300_013)
    chat = await chat_factory(telegram_id=-9_300_013)
    quote = _quote(
        operation_id=uuid4(),
        requester_id=user.id,
        chat_id=chat.id,
    )
    db_session.add(quote)
    await db_session.flush()
    db_session.add(PaidOperation(id=uuid4(), quote_id=quote.id))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_quote_rows_are_database_immutable(
    db_session, user_factory, chat_factory
) -> None:
    user = await user_factory(telegram_id=9_300_014)
    chat = await chat_factory(telegram_id=-9_300_014)
    quote = _quote(
        operation_id=uuid4(),
        requester_id=user.id,
        chat_id=chat.id,
    )
    db_session.add(quote)
    await db_session.flush()
    quote.amount_credits += 1

    with pytest.raises(DBAPIError, match="immutable billing record"):
        await db_session.flush()


async def test_allocation_cannot_cross_wallets(
    db_session, user_factory, chat_factory
) -> None:
    user = await user_factory(telegram_id=9_300_015)
    other_user = await user_factory(telegram_id=9_300_016)
    chat = await chat_factory(telegram_id=-9_300_015)
    wallet = await _create_wallet(db_session, user_id=user.id)
    other_wallet = await _create_wallet(db_session, user_id=other_user.id)
    other_lot = WalletLot(
        wallet_id=other_wallet.id,
        kind="purchased",
        granted_credits=10,
        available_credits=10,
    )
    quote = _quote(
        operation_id=uuid4(),
        requester_id=user.id,
        chat_id=chat.id,
    )
    db_session.add_all([other_lot, quote])
    await db_session.flush()
    operation = PaidOperation(
        id=quote.operation_id,
        quote_id=quote.id,
        wallet_id=wallet.id,
        funding_authorization="private",
    )
    db_session.add(operation)
    await db_session.flush()
    db_session.add(
        OperationAllocation(
            operation_id=operation.id,
            wallet_lot_id=other_lot.id,
            wallet_id=wallet.id,
            amount_credits=4,
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_allowance_owner_and_expiry_match_subscription_cycle(
    db_session, user_factory, chat_factory
) -> None:
    user = await user_factory(telegram_id=9_300_017)
    chat = await chat_factory(telegram_id=-9_300_017)
    chat_wallet = await _create_wallet(db_session, chat_id=chat.id)
    cycle = await _create_allowance_source(db_session, user.id)
    db_session.add(
        WalletLot(
            wallet_id=chat_wallet.id,
            kind="allowance",
            subscription_cycle_id=cycle.id,
            granted_credits=10,
            available_credits=10,
            expires_at=cycle.period_end,
        )
    )

    with pytest.raises(IntegrityError, match="allowance lot owner"):
        await db_session.flush()


async def test_operation_requires_an_existing_quote(db_session) -> None:
    db_session.add(
        PaidOperation(
            id=uuid4(),
            quote_id=uuid4(),
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_one_quote_cannot_back_multiple_operations(
    db_session, user_factory, chat_factory
) -> None:
    user = await user_factory(telegram_id=9_300_010)
    chat = await chat_factory(telegram_id=-9_300_010)
    operation_id = uuid4()
    quote = _quote(
        operation_id=operation_id,
        requester_id=user.id,
        chat_id=chat.id,
    )
    db_session.add(quote)
    await db_session.flush()
    db_session.add(
        PaidOperation(
            id=operation_id,
            quote_id=quote.id,
        )
    )
    await db_session.flush()
    db_session.add(
        PaidOperation(
            id=uuid4(),
            quote_id=quote.id,
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.parametrize(
    ("event_type", "amount", "available_after"),
    [
        ("unknown", 1, 0),
        ("grant", 0, 0),
        ("grant", 1, -1),
    ],
    ids=["unknown-event", "nonpositive-amount", "negative-snapshot"],
)
async def test_ledger_rejects_invalid_events_and_snapshots(
    db_session,
    user_factory,
    event_type,
    amount,
    available_after,
) -> None:
    user = await user_factory(telegram_id=9_300_011)
    wallet = await _create_wallet(db_session, user_id=user.id)
    db_session.add(
        WalletLedgerEntry(
            wallet_id=wallet.id,
            event_type=event_type,
            amount_credits=amount,
            available_after=available_after,
            reserved_after=0,
            consumed_after=0,
            wallet_debt_after=0,
            idempotency_key=f"ledger:{uuid4()}",
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_ledger_idempotency_key_is_unique(db_session, user_factory) -> None:
    user = await user_factory(telegram_id=9_300_012)
    wallet = await _create_wallet(db_session, user_id=user.id)
    values = {
        "wallet_id": wallet.id,
        "event_type": "grant",
        "amount_credits": 10,
        "available_after": 10,
        "reserved_after": 0,
        "consumed_after": 0,
        "wallet_debt_after": 0,
        "idempotency_key": f"ledger:{uuid4()}",
    }
    db_session.add(WalletLedgerEntry(**values))
    await db_session.flush()
    db_session.add(WalletLedgerEntry(**values))

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_ledger_rows_are_database_append_only(db_session, user_factory) -> None:
    user = await user_factory(telegram_id=9_300_018)
    wallet = await _create_wallet(db_session, user_id=user.id)
    entry = WalletLedgerEntry(
        wallet_id=wallet.id,
        event_type="grant",
        amount_credits=10,
        available_after=10,
        reserved_after=0,
        consumed_after=0,
        wallet_debt_after=0,
        idempotency_key=f"ledger:{uuid4()}",
    )
    db_session.add(entry)
    await db_session.flush()
    entry.amount_credits = 9

    with pytest.raises(DBAPIError, match="immutable billing record"):
        await db_session.flush()
