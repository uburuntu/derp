"""Typed commands and outcomes for durable Stars commerce."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Self


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


class ProductKind(StrEnum):
    """Commercial product families with different fulfillment semantics."""

    TOP_UP = "top_up"
    SUBSCRIPTION = "subscription"


class PurchaseTargetKind(StrEnum):
    """Wallet owner selected when an intent is created."""

    USER = "user"
    CHAT = "chat"


@dataclass(frozen=True, slots=True)
class PurchaseTarget:
    """Exactly one local wallet owner receiving purchased value."""

    kind: PurchaseTargetKind
    id: uuid.UUID

    @classmethod
    def user(cls, user_id: uuid.UUID) -> Self:
        return cls(PurchaseTargetKind.USER, user_id)

    @classmethod
    def chat(cls, chat_id: uuid.UUID) -> Self:
        return cls(PurchaseTargetKind.CHAT, chat_id)


@dataclass(frozen=True, slots=True)
class PurchaseIntentHandle:
    """Opaque invoice payload plus immutable terms safe for Telegram adapters."""

    intent_id: uuid.UUID
    invoice_payload: str
    product_kind: ProductKind
    product_id: str
    product_version: str
    target: PurchaseTarget
    credits: int
    stars: int
    currency: str
    expires_at: datetime
    subscription_period_seconds: int | None

    def __post_init__(self) -> None:
        if not self.invoice_payload or len(self.invoice_payload.encode()) > 128:
            raise ValueError("invoice_payload must contain between 1 and 128 bytes")
        _require_aware(self.expires_at, "expires_at")


class PreCheckoutRejection(StrEnum):
    """Stable reasons an adapter can translate into a checkout rejection."""

    UNKNOWN_INTENT = "unknown_intent"
    INVALID_STATUS = "invalid_status"
    EXPIRED = "expired"
    PAYER_MISMATCH = "payer_mismatch"
    TARGET_MISMATCH = "target_mismatch"
    CURRENCY_MISMATCH = "currency_mismatch"
    AMOUNT_MISMATCH = "amount_mismatch"
    PRODUCT_MISMATCH = "product_mismatch"
    ACTIVE_SUBSCRIPTION = "active_subscription"


@dataclass(frozen=True, slots=True)
class PreCheckoutRequest:
    """Allowlisted Telegram pre-checkout fields used for validation."""

    invoice_payload: str
    payer_telegram_id: int
    currency: str
    total_amount: int

    def __post_init__(self) -> None:
        if not self.invoice_payload:
            raise ValueError("invoice_payload must not be empty")
        if self.total_amount <= 0:
            raise ValueError("total_amount must be positive")


@dataclass(frozen=True, slots=True)
class PreCheckoutDecision:
    """Idempotent checkout decision with no Telegram side effects."""

    approved: bool
    intent_id: uuid.UUID | None = None
    rejection: PreCheckoutRejection | None = None

    def __post_init__(self) -> None:
        if self.approved != (self.intent_id is not None and self.rejection is None):
            raise ValueError("pre-checkout decision fields are inconsistent")


@dataclass(frozen=True, slots=True)
class CapturedPayment:
    """Allowlisted SuccessfulPayment fields independent of aiogram types."""

    invoice_payload: str
    telegram_charge_id: str
    provider_charge_id: str
    payer_telegram_id: int
    currency: str
    total_amount: int
    is_recurring: bool = False
    is_first_recurring: bool = False
    subscription_expiration_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("invoice_payload", "telegram_charge_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be blank")
        if self.total_amount <= 0:
            raise ValueError("total_amount must be positive")
        if self.is_first_recurring and not self.is_recurring:
            raise ValueError("a first recurring payment must be recurring")
        if self.subscription_expiration_at is not None:
            _require_aware(
                self.subscription_expiration_at,
                "subscription_expiration_at",
            )
            if not self.is_recurring:
                raise ValueError("subscription expiration requires a recurring payment")


class FulfillmentState(StrEnum):
    """Terminal local handling state for one captured charge."""

    FULFILLED = "fulfilled"
    NEEDS_REVIEW = "needs_review"
    CLAWED_BACK = "clawed_back"


@dataclass(frozen=True, slots=True)
class FulfillmentResult:
    """Receipt outcome returned without leaking persistence models."""

    receipt_id: uuid.UUID
    state: FulfillmentState
    intent_id: uuid.UUID | None = None
    wallet_id: uuid.UUID | None = None
    wallet_lot_id: uuid.UUID | None = None
    subscription_id: uuid.UUID | None = None
    subscription_cycle_id: uuid.UUID | None = None
    available_credits: int = 0
    debt_offset_credits: int = 0
    idempotent: bool = False
    review_reason: str | None = None


@dataclass(frozen=True, slots=True)
class SubscriptionStateResult:
    """Renewal state change that never shortens the current paid cycle."""

    subscription_id: uuid.UUID
    renewal_enabled: bool
    current_period_end: datetime
    changed: bool


class SubscriptionStatus(StrEnum):
    """Locally observed lifecycle state for a personal plan."""

    ACTIVE = "active"
    CANCELED = "canceled"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class SubscriptionManagementSnapshot:
    """Framework-independent inputs needed to render and control one plan."""

    subscription_id: uuid.UUID
    user_id: uuid.UUID
    payer_telegram_id: int
    plan_id: str
    plan_version: str
    status: SubscriptionStatus
    renewal_enabled: bool
    current_period_end: datetime
    telegram_payment_charge_id: str

    def __post_init__(self) -> None:
        if self.payer_telegram_id <= 0:
            raise ValueError("payer_telegram_id must be positive")
        for name in ("plan_id", "plan_version", "telegram_payment_charge_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be blank")
        _require_aware(self.current_period_end, "current_period_end")


@dataclass(frozen=True, slots=True)
class SubscriptionRenewalCommand:
    """Allowlisted provider command with no persistence or aiogram objects."""

    payer_telegram_id: int
    telegram_payment_charge_id: str
    enabled: bool

    def __post_init__(self) -> None:
        if self.payer_telegram_id <= 0:
            raise ValueError("payer_telegram_id must be positive")
        if not self.telegram_payment_charge_id.strip():
            raise ValueError("telegram_payment_charge_id must not be blank")


@dataclass(frozen=True, slots=True)
class ClawbackResult:
    """Source-specific purchase clawback outcome."""

    receipt_id: uuid.UUID
    wallet_id: uuid.UUID | None
    removed_available_credits: int
    debt_created_credits: int
    idempotent: bool


class CommerceError(RuntimeError):
    """Base class for violated durable-commerce invariants."""


class UnknownProductError(CommerceError):
    """An intent references a product version absent from the catalog."""


class ActiveSubscriptionError(CommerceError):
    """A second personal subscription would overlap an active cycle."""


class PaymentConflictError(CommerceError):
    """One Telegram charge ID was replayed with different immutable fields."""


class SubscriptionStateError(CommerceError):
    """A requested subscription state transition is invalid."""


__all__ = [
    "ActiveSubscriptionError",
    "CapturedPayment",
    "ClawbackResult",
    "CommerceError",
    "FulfillmentResult",
    "FulfillmentState",
    "PaymentConflictError",
    "PreCheckoutDecision",
    "PreCheckoutRejection",
    "PreCheckoutRequest",
    "ProductKind",
    "PurchaseIntentHandle",
    "PurchaseTarget",
    "PurchaseTargetKind",
    "SubscriptionManagementSnapshot",
    "SubscriptionRenewalCommand",
    "SubscriptionStateError",
    "SubscriptionStateResult",
    "SubscriptionStatus",
    "UnknownProductError",
]
