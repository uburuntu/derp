"""Short-lived in-memory capabilities for operator maintenance confirmation."""

from __future__ import annotations

import math
import secrets
import string
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Final

DEFAULT_CONFIRMATION_TTL: Final = timedelta(minutes=2)
DEFAULT_MAX_CONFIRMATIONS: Final = 1_024
MAX_CONFIRMATION_TTL: Final = timedelta(minutes=10)
MAX_CONFIRMATIONS: Final = 4_096
MAX_CONFIRMATION_TOKEN_LENGTH: Final = 46
_TOKEN_BYTES: Final = 16
_TOKEN_ATTEMPTS: Final = 8
_TOKEN_ALPHABET: Final = frozenset(string.ascii_letters + string.digits + "-_")

type MonotonicClock = Callable[[], float]
type TokenFactory = Callable[[], str]


class OperatorConfirmationCapacityError(RuntimeError):
    """The bounded confirmation store has no free entry."""


@dataclass(frozen=True, slots=True)
class _PendingConfirmation:
    actor_id: int
    action_key: str
    resource_key: str | None
    expires_at: float


@dataclass(slots=True)
class OperatorConfirmationStore:
    """Issue actor-bound, action-bound, expiring single-use capabilities."""

    ttl: timedelta = DEFAULT_CONFIRMATION_TTL
    max_entries: int = DEFAULT_MAX_CONFIRMATIONS
    clock: MonotonicClock = time.monotonic
    token_factory: TokenFactory = field(
        default=lambda: secrets.token_urlsafe(_TOKEN_BYTES),
        repr=False,
    )
    _entries: dict[str, _PendingConfirmation] = field(
        init=False,
        default_factory=dict,
        repr=False,
    )
    _ttl_seconds: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.ttl, timedelta):
            raise TypeError("ttl must be a timedelta")
        self._ttl_seconds = self.ttl.total_seconds()
        if not 0 < self._ttl_seconds <= MAX_CONFIRMATION_TTL.total_seconds():
            raise ValueError("ttl must be positive and at most ten minutes")
        if (
            isinstance(self.max_entries, bool)
            or not isinstance(self.max_entries, int)
            or not 1 <= self.max_entries <= MAX_CONFIRMATIONS
        ):
            raise ValueError(f"max_entries must be between 1 and {MAX_CONFIRMATIONS}")
        if not callable(self.clock) or not callable(self.token_factory):
            raise TypeError("clock and token_factory must be callable")

    def issue(
        self,
        *,
        actor_id: int,
        action: StrEnum,
        resource_key: str | None = None,
    ) -> str:
        """Issue one opaque confirmation token within the configured bounds."""
        self._require_actor(actor_id)
        action_key = self._require_action(action)
        self._require_resource_key(resource_key)
        now = self._now()
        self._prune(now)
        if len(self._entries) >= self.max_entries:
            raise OperatorConfirmationCapacityError(
                "operator confirmation capacity is exhausted"
            )

        for _ in range(_TOKEN_ATTEMPTS):
            token = self.token_factory()
            self._require_token(token)
            if token not in self._entries:
                self._entries[token] = _PendingConfirmation(
                    actor_id=actor_id,
                    action_key=action_key,
                    resource_key=resource_key,
                    expires_at=now + self._ttl_seconds,
                )
                return token
        raise OperatorConfirmationCapacityError(
            "operator confirmation token generation was exhausted"
        )

    def consume(
        self,
        token: str,
        *,
        actor_id: int,
        action: StrEnum,
    ) -> bool:
        """Consume an exact live capability once, failing closed on mismatch."""
        action_key = self._action_key(action)
        if not self._valid_actor(actor_id) or action_key is None:
            return False
        if not self._valid_token(token):
            return False

        now = self._now()
        self._prune(now)
        pending = self._entries.get(token)
        if pending is None:
            return False
        if pending.actor_id != actor_id or pending.action_key != action_key:
            return False
        del self._entries[token]
        return True

    def consume_resource(
        self,
        token: str,
        *,
        actor_id: int,
        action: StrEnum,
    ) -> str | None:
        """Consume a capability and return its server-bound resource identity."""
        action_key = self._action_key(action)
        if not self._valid_actor(actor_id) or action_key is None:
            return None
        if not self._valid_token(token):
            return None

        now = self._now()
        self._prune(now)
        pending = self._entries.get(token)
        if (
            pending is None
            or pending.actor_id != actor_id
            or pending.action_key != action_key
            or pending.resource_key is None
        ):
            return None
        del self._entries[token]
        return pending.resource_key

    def prune(self) -> int:
        """Remove expired capabilities and return the number discarded."""
        return self._prune(self._now())

    @property
    def pending_count(self) -> int:
        """Return the live bounded entry count after expiry pruning."""
        self._prune(self._now())
        return len(self._entries)

    def _prune(self, now: float) -> int:
        expired = [
            token
            for token, pending in self._entries.items()
            if pending.expires_at <= now
        ]
        for token in expired:
            del self._entries[token]
        return len(expired)

    def _now(self) -> float:
        now = self.clock()
        if isinstance(now, bool) or not isinstance(now, int | float):
            raise TypeError("clock must return a number")
        if not math.isfinite(now):
            raise ValueError("clock must return a finite value")
        return float(now)

    @staticmethod
    def _valid_actor(actor_id: object) -> bool:
        return (
            isinstance(actor_id, int)
            and not isinstance(actor_id, bool)
            and actor_id > 0
        )

    @classmethod
    def _require_actor(cls, actor_id: object) -> None:
        if not cls._valid_actor(actor_id):
            raise ValueError("actor_id must be a positive integer")

    @classmethod
    def _require_action(cls, action: object) -> str:
        if (action_key := cls._action_key(action)) is None:
            raise TypeError("action must be a StrEnum")
        return action_key

    @staticmethod
    def _require_resource_key(resource_key: object) -> None:
        if resource_key is None:
            return
        if (
            not isinstance(resource_key, str)
            or not 1 <= len(resource_key) <= 64
            or not resource_key.isascii()
            or any(character.isspace() for character in resource_key)
        ):
            raise ValueError(
                "resource_key must be 1-64 non-whitespace ASCII characters"
            )

    @staticmethod
    def _action_key(action: object) -> str | None:
        if not isinstance(action, StrEnum):
            return None
        return f"{type(action).__module__}.{type(action).__qualname__}:{action.value}"

    @classmethod
    def _valid_token(cls, token: object) -> bool:
        return (
            isinstance(token, str)
            and 1 <= len(token) <= MAX_CONFIRMATION_TOKEN_LENGTH
            and token.isascii()
            and all(character in _TOKEN_ALPHABET for character in token)
        )

    @classmethod
    def _require_token(cls, token: object) -> None:
        if not cls._valid_token(token):
            raise RuntimeError("confirmation generator returned an invalid token")


__all__ = [
    "DEFAULT_CONFIRMATION_TTL",
    "DEFAULT_MAX_CONFIRMATIONS",
    "MAX_CONFIRMATIONS",
    "MAX_CONFIRMATION_TOKEN_LENGTH",
    "MAX_CONFIRMATION_TTL",
    "OperatorConfirmationCapacityError",
    "OperatorConfirmationStore",
]
