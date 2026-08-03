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

from derp.artifacts import ArtifactKind, ArtifactTooLargeError
from derp.media.types import normalize_mime_type
from derp.operations.types import DeliveryState, OperationId

MAX_TELEGRAM_PHOTO_BYTES = 10 * 1024 * 1024
MAX_TELEGRAM_FILE_BYTES = 50 * 1024 * 1024
MAX_TELEGRAM_ALBUM_ITEMS = 10

_PHOTO_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_VIDEO_MIME_TYPES = frozenset({"video/mp4"})
_AUDIO_MIME_TYPES = frozenset(
    {"audio/m4a", "audio/mp3", "audio/mp4", "audio/mpeg", "audio/x-m4a"}
)
_VOICE_MIME_TYPES = _AUDIO_MIME_TYPES | {"audio/ogg"}


class TelegramMediaKind(StrEnum):
    """How Telegram must present one persisted artifact."""

    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    DOCUMENT = "document"

    @property
    def artifact_kind(self) -> ArtifactKind:
        """Return the storage kind persisted with this presentation."""
        if self is TelegramMediaKind.PHOTO:
            return ArtifactKind.IMAGE
        return ArtifactKind(self.value)

    @property
    def max_size_bytes(self) -> int:
        """Return Telegram's upload limit for this presentation."""
        if self is TelegramMediaKind.PHOTO:
            return MAX_TELEGRAM_PHOTO_BYTES
        return MAX_TELEGRAM_FILE_BYTES

    def accepts_mime_type(self, mime_type: str) -> bool:
        """Return whether Telegram documents this MIME for this presentation."""
        if self is TelegramMediaKind.PHOTO:
            return mime_type in _PHOTO_MIME_TYPES
        if self is TelegramMediaKind.VIDEO:
            return mime_type in _VIDEO_MIME_TYPES
        if self is TelegramMediaKind.AUDIO:
            return mime_type in _AUDIO_MIME_TYPES
        if self is TelegramMediaKind.VOICE:
            return mime_type in _VOICE_MIME_TYPES
        return True

    @classmethod
    def from_artifact_kind(cls, kind: ArtifactKind) -> TelegramMediaKind:
        """Recover Telegram presentation from durable artifact metadata."""
        if kind is ArtifactKind.IMAGE:
            return cls.PHOTO
        return cls(kind.value)


@dataclass(frozen=True, slots=True)
class DeliveryMedia:
    """Provider-neutral bytes plus their intended Telegram presentation."""

    kind: TelegramMediaKind
    mime_type: str
    data: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TelegramMediaKind):
            raise TypeError("delivery media kind must be a TelegramMediaKind")
        if not isinstance(self.data, bytes):
            raise TypeError("delivery media data must be immutable bytes")
        if not self.data:
            raise ValueError("delivery media data must not be empty")
        mime_type = normalize_mime_type(self.mime_type)
        if not self.kind.accepts_mime_type(mime_type):
            raise ValueError("delivery media MIME type does not match its presentation")
        if len(self.data) > self.kind.max_size_bytes:
            raise ArtifactTooLargeError(
                size_bytes=len(self.data),
                limit_bytes=self.kind.max_size_bytes,
            )
        object.__setattr__(self, "mime_type", mime_type)

    @property
    def artifact_kind(self) -> ArtifactKind:
        """Return the durable storage kind for this item."""
        return self.kind.artifact_kind


class DeliveryBatchKind(StrEnum):
    """The single Bot API request shape for one delivery attempt."""

    SINGLE = "single"
    VISUAL_ALBUM = "visual_album"
    AUDIO_ALBUM = "audio_album"
    DOCUMENT_ALBUM = "document_album"


def classify_delivery_batch(
    kinds: tuple[TelegramMediaKind, ...],
) -> DeliveryBatchKind:
    """Validate a delivery and select its one-call Telegram request shape."""
    if not kinds:
        raise ValueError("delivery result must contain media")
    if len(kinds) == 1:
        return DeliveryBatchKind.SINGLE
    if len(kinds) > MAX_TELEGRAM_ALBUM_ITEMS:
        raise ValueError(
            f"Telegram albums support at most {MAX_TELEGRAM_ALBUM_ITEMS} items"
        )
    unique_kinds = frozenset(kinds)
    if unique_kinds <= {TelegramMediaKind.PHOTO, TelegramMediaKind.VIDEO}:
        return DeliveryBatchKind.VISUAL_ALBUM
    if unique_kinds == {TelegramMediaKind.AUDIO}:
        return DeliveryBatchKind.AUDIO_ALBUM
    if unique_kinds == {TelegramMediaKind.DOCUMENT}:
        return DeliveryBatchKind.DOCUMENT_ALBUM
    raise ValueError("delivery media cannot be sent in one Telegram album")


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
class ResendCallbackAuthorization:
    """Telegram-authenticated context for an opaque resend capability."""

    token: str
    actor_user_id: int
    chat_id: int
    thread_id: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.token, str) or not self.token.strip():
            raise ValueError("resend token must not be blank")
        if len(self.token) > 128:
            raise ValueError("resend token is too long")
        for name in ("actor_user_id", "chat_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if name == "actor_user_id" and value <= 0:
                raise ValueError("actor_user_id must be positive")
            if name == "chat_id" and value == 0:
                raise ValueError("chat_id must not be zero")
        if self.thread_id is not None:
            if isinstance(self.thread_id, bool) or not isinstance(self.thread_id, int):
                raise TypeError("thread_id must be an integer")
            if self.thread_id <= 0:
                raise ValueError("thread_id must be positive")


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
    failed_count: int = 0

    def __post_init__(self) -> None:
        if any(not isinstance(value, OperationId) for value in self.operation_ids):
            raise TypeError("reconciliation IDs must be OperationId values")
        if (
            isinstance(self.failed_count, bool)
            or not isinstance(self.failed_count, int)
            or not 0 <= self.failed_count <= len(self.operation_ids)
        ):
            raise ValueError(
                "reconciliation failures must not exceed reconciled operations"
            )

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


@dataclass(frozen=True, slots=True)
class ResendResult:
    """Server-resolved resend identity and resulting delivery outcome."""

    operation_id: OperationId
    target: DeliveryTarget
    outcome: DeliveryOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, OperationId):
            raise TypeError("resend operation_id must be an OperationId")
        if not isinstance(self.target, DeliveryTarget):
            raise TypeError("resend target must be a DeliveryTarget")
        if not isinstance(
            self.outcome,
            (Delivered, DeliveryFailed, DeliveryUncertain),
        ):
            raise TypeError("resend outcome must be a DeliveryOutcome")


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
