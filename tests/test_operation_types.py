"""Paid-operation domain values make idempotency and ownership explicit."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from derp.catalog import GoogleModelKey
from derp.execution import Feature
from derp.operations import (
    ContextBand,
    DeliveryState,
    InventoryAllocation,
    OperationId,
    OperationState,
    Quote,
    QuoteId,
    QuoteKey,
    WalletOwner,
    WalletOwnerKind,
)


def test_operation_ids_are_retry_stable_and_tool_call_specific() -> None:
    command = OperationId.for_command(
        feature=Feature.IMAGE_GENERATE,
        chat_id=-1001,
        message_id=42,
    )
    retried = OperationId.for_command(
        feature=Feature.IMAGE_GENERATE,
        chat_id=-1001,
        message_id=42,
    )
    first_tool = OperationId.for_tool(
        feature=Feature.IMAGE_GENERATE,
        chat_id=-1001,
        message_id=42,
        tool_call_id="call-1",
    )
    second_tool = OperationId.for_tool(
        feature=Feature.IMAGE_GENERATE,
        chat_id=-1001,
        message_id=42,
        tool_call_id="call-2",
    )

    assert command == retried
    assert len({command, first_tool, second_tool}) == 3


@pytest.mark.parametrize(
    ("tokens", "band"),
    [
        (0, ContextBand.SMALL),
        (8_001, ContextBand.MEDIUM),
        (32_001, ContextBand.LARGE),
        (128_001, ContextBand.MAXIMUM),
    ],
)
def test_context_bands_are_deterministic(tokens: int, band: ContextBand) -> None:
    assert ContextBand.for_input_tokens(tokens) is band


def test_quote_is_fixed_versioned_and_expires_closed() -> None:
    now = datetime(2026, 7, 20, tzinfo=UTC)
    quote = Quote(
        id=QuoteId.new(),
        operation_id=OperationId.for_command(
            feature=Feature.CHAT,
            chat_id=1,
            message_id=1,
        ),
        key=QuoteKey(
            feature=Feature.CHAT,
            model_key=GoogleModelKey.CHAT_STANDARD,
            context_band=ContextBand.MEDIUM,
        ),
        credits=12,
        estimated_provider_cost_usd=Decimal("0.007"),
        created_at=now,
        expires_at=now + timedelta(minutes=10),
        pricing_version="2026-07-20",
        catalog_verified_on=date(2026, 7, 20),
    )

    assert quote.is_active_at(now)
    assert not quote.is_active_at(quote.expires_at)


def test_allocation_can_mix_inventories_but_has_one_owner() -> None:
    owner = WalletOwner(WalletOwnerKind.USER, UUID(int=1))
    allocation = InventoryAllocation(
        owner=owner,
        allowance_credits=7,
        purchased_credits=5,
    )

    assert allocation.total_credits == 12
    assert allocation.owner is owner
    with pytest.raises(ValueError, match="must reserve"):
        InventoryAllocation(owner, allowance_credits=0, purchased_credits=0)


def test_terminal_states_are_explicit() -> None:
    assert OperationState.CAPTURED.terminal is False
    assert OperationState.REVERSED.terminal is True
    assert DeliveryState.UNCERTAIN.terminal is False
    assert DeliveryState.DELIVERED.terminal is True
