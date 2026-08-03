"""Immutable values for user-visible AI run receipts."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

from derp.inference_usage import InferenceTokenUsage


class RunPrivacyMode(StrEnum):
    PRIVATE = "private"
    FREE = "free"


@dataclass(frozen=True, slots=True)
class ChatRunDelivery:
    """Content-free facts captured after Telegram accepts a chat answer."""

    operation_id: uuid.UUID
    chat_id: uuid.UUID
    requester_id: uuid.UUID
    request_message_id: int
    response_message_ids: tuple[int, ...]
    model_key: str
    model_display_name: str
    privacy_mode: RunPrivacyMode
    context_messages: int
    context_turns: int
    context_estimated_tokens: int

    def __post_init__(self) -> None:
        for name in ("operation_id", "chat_id", "requester_id"):
            if not isinstance(getattr(self, name), uuid.UUID):
                raise TypeError(f"{name} must be a UUID")
        if self.request_message_id <= 0:
            raise ValueError("request_message_id must be positive")
        if not self.response_message_ids or any(
            message_id <= 0 for message_id in self.response_message_ids
        ):
            raise ValueError("response_message_ids must be positive and nonempty")
        if len(set(self.response_message_ids)) != len(self.response_message_ids):
            raise ValueError("response_message_ids must be unique")
        for name in ("model_key", "model_display_name"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must not be blank")
        if not isinstance(self.privacy_mode, RunPrivacyMode):
            raise TypeError("privacy_mode must be a RunPrivacyMode")
        for name in (
            "context_messages",
            "context_turns",
            "context_estimated_tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class RunInfo:
    """User-safe projection of one delivered AI operation."""

    model_display_name: str
    privacy_mode: RunPrivacyMode
    tokens: InferenceTokenUsage | None
    context_messages: int | None
    context_turns: int | None
    context_estimated_tokens: int | None
    charged_credits: int | None
    charge_visible: bool = True


__all__ = ["ChatRunDelivery", "RunInfo", "RunPrivacyMode"]
