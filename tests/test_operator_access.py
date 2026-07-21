"""Access and confirmation boundaries for the private operator console."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User

from derp.operator import (
    MAX_CONFIRMATION_TOKEN_LENGTH,
    OperatorAccessPolicy,
    OperatorConfirmationCapacityError,
    OperatorConfirmationStore,
    OperatorMaintenanceAction,
    OperatorOnlyFilter,
)


class MutableClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def telegram_user(user_id: int) -> User:
    return User(id=user_id, is_bot=False, first_name="Operator")


def telegram_message(user: User | None) -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=1, type="private"),
        from_user=user,
    )


def telegram_callback(user: User) -> CallbackQuery:
    return CallbackQuery(
        id="callback",
        from_user=user,
        chat_instance="private",
        data="operator:overview",
    )


def test_access_policy_copies_and_deduplicates_positive_ids() -> None:
    operator_ids = [11, 22, 11]
    policy = OperatorAccessPolicy.from_ids(operator_ids)
    operator_ids.clear()

    assert policy.allows(11)
    assert policy.allows(22)
    assert not policy.allows(33)
    assert not policy.allows(None)
    assert not policy.allows(True)


@pytest.mark.parametrize("operator_id", [True, 0, -1, "11"])
def test_access_policy_rejects_invalid_ids(operator_id: object) -> None:
    with pytest.raises(ValueError, match="positive integers"):
        OperatorAccessPolicy.from_ids([operator_id])  # type: ignore[list-item]


@pytest.mark.parametrize("event_factory", [telegram_message, telegram_callback])
async def test_operator_filter_authorizes_message_and_callback_actors(
    event_factory,
) -> None:
    policy = OperatorAccessPolicy.from_ids([42])
    operator_filter = OperatorOnlyFilter()

    assert await operator_filter(
        event_factory(telegram_user(42)),
        operator_access=policy,
    )
    assert not await operator_filter(
        event_factory(telegram_user(41)),
        operator_access=policy,
    )


async def test_operator_filter_fails_closed_without_message_actor() -> None:
    assert not await OperatorOnlyFilter()(
        telegram_message(None),
        operator_access=OperatorAccessPolicy.from_ids([42]),
    )


def test_confirmation_is_opaque_bound_and_single_use() -> None:
    store = OperatorConfirmationStore()
    token = store.issue(
        actor_id=42,
        action=OperatorMaintenanceAction.OPERATIONS,
    )

    assert 20 <= len(token) <= MAX_CONFIRMATION_TOKEN_LENGTH
    assert re.fullmatch(r"[A-Za-z0-9_-]+", token)
    assert not store.consume(
        token,
        actor_id=41,
        action=OperatorMaintenanceAction.OPERATIONS,
    )
    assert not store.consume(
        token,
        actor_id=42,
        action=OperatorMaintenanceAction.DELIVERIES,
    )
    assert store.consume(
        token,
        actor_id=42,
        action=OperatorMaintenanceAction.OPERATIONS,
    )
    assert not store.consume(
        token,
        actor_id=42,
        action=OperatorMaintenanceAction.OPERATIONS,
    )


def test_confirmation_expires_at_monotonic_deadline() -> None:
    clock = MutableClock(10.0)
    store = OperatorConfirmationStore(
        ttl=timedelta(seconds=5),
        clock=clock,
        token_factory=lambda: "fixed_confirmation_token",
    )
    token = store.issue(actor_id=42, action=OperatorMaintenanceAction.HISTORY)

    clock.now = 15.0

    assert not store.consume(
        token,
        actor_id=42,
        action=OperatorMaintenanceAction.HISTORY,
    )
    assert store.pending_count == 0


def test_confirmation_rejects_tokens_too_long_for_callback_envelope() -> None:
    store = OperatorConfirmationStore(
        token_factory=lambda: "x" * (MAX_CONFIRMATION_TOKEN_LENGTH + 1)
    )

    with pytest.raises(RuntimeError, match="invalid token"):
        store.issue(actor_id=42, action=OperatorMaintenanceAction.SUBSCRIPTIONS)


def test_confirmation_capacity_is_strict_and_expired_entries_are_pruned() -> None:
    clock = MutableClock()
    tokens = iter(
        [
            "confirmation_token_one",
            "confirmation_token_two",
            "confirmation_token_three",
        ]
    )
    store = OperatorConfirmationStore(
        ttl=timedelta(seconds=1),
        max_entries=2,
        clock=clock,
        token_factory=lambda: next(tokens),
    )
    store.issue(actor_id=1, action=OperatorMaintenanceAction.HISTORY)
    store.issue(actor_id=2, action=OperatorMaintenanceAction.APPROVALS)

    with pytest.raises(OperatorConfirmationCapacityError, match="capacity"):
        store.issue(actor_id=3, action=OperatorMaintenanceAction.DELIVERIES)

    clock.now = 1.0
    token = store.issue(actor_id=3, action=OperatorMaintenanceAction.DELIVERIES)

    assert token == "confirmation_token_three"
    assert store.pending_count == 1


@pytest.mark.parametrize(
    ("ttl", "max_entries"),
    [
        (timedelta(0), 1),
        (timedelta(minutes=10, microseconds=1), 1),
        (timedelta(seconds=1), False),
        (timedelta(seconds=1), 0),
        (timedelta(seconds=1), 4_097),
    ],
)
def test_confirmation_store_rejects_unbounded_configuration(
    ttl: timedelta,
    max_entries: int,
) -> None:
    with pytest.raises(ValueError):
        OperatorConfirmationStore(ttl=ttl, max_entries=max_entries)
