"""Provider-independent Telegram delivery values and certainty classification."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
)


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
    if isinstance(error, (TelegramNetworkError, TimeoutError)):
        return DeliveryUncertain(code)
    if isinstance(error, TelegramRetryAfter):
        return DeliveryFailed(code, retryable=True)
    if isinstance(error, TelegramAPIError):
        return DeliveryFailed(code, retryable=False)
    return DeliveryUncertain(code)
