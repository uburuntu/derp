"""Short-lived in-memory capabilities for operator maintenance confirmation."""

from __future__ import annotations

import math
import secrets
import string
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Final

from derp.operator.types import OperatorMaintenanceAction

DEFAULT_CONFIRMATION_TTL: Final = timedelta(minutes=2)
DEFAULT_MAX_CONFIRMATIONS: Final = 1_024
MAX_CONFIRMATION_TTL: Final = timedelta(minutes=10)
MAX_CONFIRMATIONS: Final = 4_096
_TOKEN_BYTES: Final = 16
_MAX_TOKEN_LENGTH: Final = 64
_TOKEN_ATTEMPTS: Final = 8
_TOKEN_ALPHABET: Final = frozenset(string.ascii_letters + string.digits + "-_")

type MonotonicClock = Callable[[], float]
type TokenFactory = Callable[[], str]


class OperatorConfirmationCapacityError(RuntimeError):
    """The bounded confirmation store has no free entry."""


@dataclass(frozen=True, slots=True)
class _PendingConfirmation:
    actor_id: int
    action: OperatorMaintenanceAction
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

    def issue(self, *, actor_id: int, action: OperatorMaintenanceAction) -> str:
        """Issue one opaque confirmation token within the configured bounds."""
        self._require_actor(actor_id)
        self._require_action(action)
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
                    action=action,
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
        action: OperatorMaintenanceAction,
    ) -> bool:
        """Consume an exact live capability once, failing closed on mismatch."""
        if not self._valid_actor(actor_id) or not isinstance(
            action, OperatorMaintenanceAction
        ):
            return False
        if not self._valid_token(token):
            return False

        now = self._now()
        self._prune(now)
        pending = self._entries.get(token)
        if pending is None:
            return False
        if pending.actor_id != actor_id or pending.action is not action:
            return False
        del self._entries[token]
        return True

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

    @staticmethod
    def _require_action(action: object) -> None:
        if not isinstance(action, OperatorMaintenanceAction):
            raise TypeError("action must be an OperatorMaintenanceAction")

    @classmethod
    def _valid_token(cls, token: object) -> bool:
        return (
            isinstance(token, str)
            and 1 <= len(token) <= _MAX_TOKEN_LENGTH
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
    "MAX_CONFIRMATION_TTL",
    "OperatorConfirmationCapacityError",
    "OperatorConfirmationStore",
]
