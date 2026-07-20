"""Provider-independent Telegram delivery values and certainty classification."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from derp.operations.types import DeliveryState, OperationId


@dataclass(frozen=True, slots=True)
class DeliveryTarget:
    """Exact Telegram destination persisted before any send attempt."""

    chat_id: int
    thread_id: int | None
    reply_to_message_id: int | None
    business_connection_id: str | None = None

    def __post_init__(self) -> None:
        if self.chat_id == 0:
            raise ValueError("chat_id must not be zero")
        for name in ("thread_id", "reply_to_message_id"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.business_connection_id is not None and not (
            self.business_connection_id.strip()
        ):
            raise ValueError("business_connection_id must not be blank")


class ProgressStage(StrEnum):
    """Honest long-running stages without fake percentages."""

    PREPARING = "preparing"
    GENERATING = "generating"
    DELIVERING = "delivering"


@dataclass(frozen=True, slots=True)
class ResendAuthorization:
    """Opaque resend capability bound to the requester and original target."""

    token: str
    actor_user_id: int
    target: DeliveryTarget

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or not self.token.strip():
            raise ValueError("resend token must not be blank")
        if len(self.token) > 128:
            raise ValueError("resend token is too long")
        if (
            isinstance(self.actor_user_id, bool)
            or not isinstance(self.actor_user_id, int)
            or self.actor_user_id <= 0
        ):
            raise ValueError("actor_user_id must be positive")
        if not isinstance(self.target, DeliveryTarget):
            raise TypeError("resend target must be a DeliveryTarget")


@dataclass(frozen=True, slots=True)
class DeliveryInspection:
    """Content-free durable delivery state exposed to coordinators."""

    operation_id: OperationId
    state: DeliveryState
    target: DeliveryTarget
    attempt_count: int
    artifact_count: int
    expires_at: datetime
    updated_at: datetime
    message_ids: tuple[int, ...]
    last_error_code: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, OperationId):
            raise TypeError("inspection operation_id must be an OperationId")
        if not isinstance(self.state, DeliveryState):
            raise TypeError("inspection state must be a DeliveryState")
        if not isinstance(self.target, DeliveryTarget):
            raise TypeError("inspection target must be a DeliveryTarget")
        for name in ("attempt_count", "artifact_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in ("expires_at", "updated_at"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if any(value <= 0 for value in self.message_ids):
            raise ValueError("inspection message IDs must be positive")
        if self.last_error_code is not None and not self.last_error_code.strip():
            raise ValueError("last_error_code must not be blank")

    @property
    def resend_required(self) -> bool:
        """Return whether an authenticated user decision is required."""
        return self.state is DeliveryState.UNCERTAIN


@dataclass(frozen=True, slots=True)
class DeliveryReconciliation:
    """Operations changed or settled by one bounded reconciliation pass."""

    operation_ids: tuple[OperationId, ...]

    def __post_init__(self) -> None:
        if any(not isinstance(value, OperationId) for value in self.operation_ids):
            raise TypeError("reconciliation IDs must be OperationId values")

    @property
    def reconciled_count(self) -> int:
        return len(self.operation_ids)


@dataclass(frozen=True, slots=True)
class ArtifactCleanup:
    """Result of one bounded artifact cleanup batch."""

    examined_count: int
    purged_count: int
    failed_count: int

    def __post_init__(self) -> None:
        counts = (self.examined_count, self.purged_count, self.failed_count)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts
        ):
            raise ValueError("artifact cleanup counts must be non-negative integers")
        if self.purged_count + self.failed_count > self.examined_count:
            raise ValueError("artifact cleanup outcomes exceed examined artifacts")


@dataclass(frozen=True, slots=True)
class Delivered:
    """Telegram acknowledged all result messages."""

    message_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.message_ids or any(value <= 0 for value in self.message_ids):
            raise ValueError("delivered message IDs must be positive")


@dataclass(frozen=True, slots=True)
class DeliveryFailed:
    """Telegram definitively rejected a send that it did not accept."""

    code: str
    retryable: bool

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("delivery failure code must not be blank")


@dataclass(frozen=True, slots=True)
class DeliveryUncertain:
    """The request may have reached Telegram and must not auto-resend."""

    code: str

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("delivery uncertainty code must not be blank")


type DeliveryOutcome = Delivered | DeliveryFailed | DeliveryUncertain


def classify_delivery_exception(error: BaseException) -> DeliveryOutcome:
    """Classify send failures conservatively around Telegram's ambiguity gap."""
    code = type(error).__name__
    if isinstance(
        error,
        (
            asyncio.CancelledError,
            TelegramNetworkError,
            TelegramServerError,
            TimeoutError,
        ),
    ):
        return DeliveryUncertain(code)
    if isinstance(error, TelegramRetryAfter):
        return DeliveryFailed(code, retryable=True)
    if isinstance(error, TelegramAPIError):
        return DeliveryFailed(code, retryable=False)
    return DeliveryUncertain(code)
