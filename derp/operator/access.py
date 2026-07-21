"""Fail-closed access policy for the private operator console."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message


@dataclass(frozen=True, slots=True, init=False)
class OperatorAccessPolicy:
    """Immutable allowlist of Telegram actors trusted as operators."""

    _operator_ids: frozenset[int] = field(repr=False)

    def __init__(self, operator_ids: Iterable[int]) -> None:
        configured_ids = tuple(operator_ids)
        if any(
            isinstance(operator_id, bool)
            or not isinstance(operator_id, int)
            or operator_id <= 0
            for operator_id in configured_ids
        ):
            raise ValueError("operator IDs must be positive integers")
        object.__setattr__(self, "_operator_ids", frozenset(configured_ids))

    @classmethod
    def from_ids(cls, operator_ids: Iterable[int]) -> OperatorAccessPolicy:
        """Copy validated actor IDs into an immutable access policy."""
        return cls(operator_ids)

    def allows(self, actor_id: int | None) -> bool:
        """Return whether one Telegram actor is explicitly allowlisted."""
        return (
            isinstance(actor_id, int)
            and not isinstance(actor_id, bool)
            and actor_id > 0
            and actor_id in self._operator_ids
        )


class OperatorOnlyFilter(BaseFilter):
    """Authorize a message or callback actor through injected operator policy."""

    async def __call__(
        self,
        event: Message | CallbackQuery,
        operator_access: OperatorAccessPolicy,
    ) -> bool:
        actor = event.from_user
        return operator_access.allows(actor and actor.id)


__all__ = ["OperatorAccessPolicy", "OperatorOnlyFilter"]
