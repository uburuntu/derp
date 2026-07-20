"""Immutable values and state vocabulary for every paid operation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final, Self

from derp.catalog import GoogleModelKey
from derp.execution import Feature

_OPERATION_NAMESPACE: Final = uuid.UUID("84eeeaac-25bb-4b87-a239-1236076990dc")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class OperationId:
    """Stable idempotency identity for one billable side effect."""

    value: uuid.UUID

    @classmethod
    def for_command(
        cls,
        *,
        feature: Feature,
        chat_id: int,
        message_id: int,
    ) -> Self:
        """Derive the same ID for retries of one Telegram command."""
        if message_id <= 0:
            raise ValueError("message_id must be positive")
        return cls(
            uuid.uuid5(
                _OPERATION_NAMESPACE,
                f"command:{feature.value}:{chat_id}:{message_id}",
            )
        )

    @classmethod
    def for_tool(
        cls,
        *,
        feature: Feature,
        chat_id: int,
        message_id: int,
        tool_call_id: str,
    ) -> Self:
        """Derive one ID per Pydantic AI tool call, including repeated tools."""
        if message_id <= 0:
            raise ValueError("message_id must be positive")
        if not tool_call_id.strip():
            raise ValueError("tool_call_id must not be blank")
        return cls(
            uuid.uuid5(
                _OPERATION_NAMESPACE,
                f"tool:{feature.value}:{chat_id}:{message_id}:{tool_call_id}",
            )
        )

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class QuoteId:
    """Opaque identity for one immutable quote."""

    value: uuid.UUID

    @classmethod
    def new(cls) -> Self:
        return cls(uuid.uuid4())

    def __str__(self) -> str:
        return str(self.value)


class ContextBand(StrEnum):
    """Fixed pricing bands for bounded model input and finishing work."""

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    MAXIMUM = "maximum"

    @classmethod
    def for_input_tokens(cls, tokens: int) -> Self:
        if tokens < 0:
            raise ValueError("input tokens must not be negative")
        if tokens <= 8_000:
            return cls.SMALL
        if tokens <= 32_000:
            return cls.MEDIUM
        if tokens <= 128_000:
            return cls.LARGE
        return cls.MAXIMUM


@dataclass(frozen=True, slots=True)
class QuoteKey:
    """Pricing identity independent from provider request variance."""

    feature: Feature
    model_key: GoogleModelKey
    context_band: ContextBand
    variant: str = "default"

    def __post_init__(self) -> None:
        if not self.variant.strip():
            raise ValueError("quote variant must not be blank")


@dataclass(frozen=True, slots=True)
class Quote:
    """One exact fixed credit price with a bounded approval lifetime."""

    id: QuoteId
    operation_id: OperationId
    key: QuoteKey
    credits: int
    estimated_provider_cost_usd: Decimal
    created_at: datetime
    expires_at: datetime
    pricing_version: str
    catalog_verified_on: date

    def __post_init__(self) -> None:
        if self.credits < 0:
            raise ValueError("quoted credits must not be negative")
        if self.estimated_provider_cost_usd < 0:
            raise ValueError("provider cost must not be negative")
        _require_aware(self.created_at, "created_at")
        _require_aware(self.expires_at, "expires_at")
        if self.expires_at <= self.created_at:
            raise ValueError("quote expiry must follow creation")
        if not self.pricing_version.strip():
            raise ValueError("pricing_version must not be blank")

    def is_active_at(self, timestamp: datetime) -> bool:
        _require_aware(timestamp, "timestamp")
        return self.created_at <= timestamp < self.expires_at


class WalletOwnerKind(StrEnum):
    """Exactly one owner funds an operation."""

    USER = "user"
    CHAT = "chat"


@dataclass(frozen=True, slots=True)
class WalletOwner:
    """Typed owner coordinate for a personal or shared wallet."""

    kind: WalletOwnerKind
    id: uuid.UUID


class InventoryKind(StrEnum):
    """Wallet inventories with distinct expiry semantics."""

    ALLOWANCE = "allowance"
    PURCHASED = "purchased"


@dataclass(frozen=True, slots=True)
class InventoryAllocation:
    """One reservation split within one wallet, allowance before purchased."""

    owner: WalletOwner
    allowance_credits: int
    purchased_credits: int

    def __post_init__(self) -> None:
        if self.allowance_credits < 0 or self.purchased_credits < 0:
            raise ValueError("inventory allocations must not be negative")
        if self.total_credits <= 0:
            raise ValueError("an inventory allocation must reserve credits")

    @property
    def total_credits(self) -> int:
        return self.allowance_credits + self.purchased_credits


class OperationState(StrEnum):
    """Durable settlement lifecycle; terminal states never reopen."""

    QUOTED = "quoted"
    RESERVED = "reserved"
    EXECUTING = "executing"
    CAPTURED = "captured"
    RELEASED = "released"
    REVERSED = "reversed"
    CANCELED = "canceled"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {
            OperationState.RELEASED,
            OperationState.REVERSED,
            OperationState.CANCELED,
            OperationState.FAILED,
        }


class DeliveryState(StrEnum):
    """Provider success is independent from Telegram delivery."""

    NOT_READY = "not_ready"
    PENDING = "pending"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    UNCERTAIN = "uncertain"
    FAILED = "failed"
    EXPIRED = "expired"

    @property
    def terminal(self) -> bool:
        return self in {
            DeliveryState.DELIVERED,
            DeliveryState.FAILED,
            DeliveryState.EXPIRED,
        }
