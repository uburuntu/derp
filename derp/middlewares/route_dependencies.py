"""Load mutable domain dependencies only for matched handler routes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from aiogram import BaseMiddleware, Router
from aiogram.types import TelegramObject

from derp.db import DatabaseManager
from derp.middlewares.commerce import CommerceDependency, CommerceMiddleware
from derp.middlewares.credit_service import CreditServiceMiddleware
from derp.middlewares.db_models import DatabaseModelMiddleware


class RouteEvent(StrEnum):
    """Aiogram observer names with mutable domain dependencies."""

    MESSAGE = "message"
    CALLBACK_QUERY = "callback_query"
    CHOSEN_INLINE_RESULT = "chosen_inline_result"
    PRE_CHECKOUT_QUERY = "pre_checkout_query"
    MY_CHAT_MEMBER = "my_chat_member"


@dataclass(frozen=True, slots=True)
class RouteDependencyKey:
    """A deepest matched router and its Telegram event observer."""

    event: RouteEvent
    router_name: str


@dataclass(frozen=True, slots=True)
class RouteDependencyPlan:
    """Mutable dependencies made available to one matched route."""

    models: bool = False
    legacy_credit: bool = False
    commerce: frozenset[CommerceDependency] = frozenset()


_MODELS = RouteDependencyPlan(models=True)

ROUTE_DEPENDENCY_PLANS: Final[Mapping[RouteDependencyKey, RouteDependencyPlan]] = (
    MappingProxyType(
        {
            RouteDependencyKey(RouteEvent.CALLBACK_QUERY, "debug"): RouteDependencyPlan(
                models=True,
                commerce=frozenset({CommerceDependency.PURCHASE_INTENTS}),
            ),
            RouteDependencyKey(RouteEvent.MESSAGE, "context_settings"): _MODELS,
            RouteDependencyKey(RouteEvent.CALLBACK_QUERY, "context_settings"): _MODELS,
            RouteDependencyKey(RouteEvent.MY_CHAT_MEMBER, "context_settings"): _MODELS,
            RouteDependencyKey(RouteEvent.MESSAGE, "legal_support"): _MODELS,
            RouteDependencyKey(RouteEvent.CALLBACK_QUERY, "legal_support"): _MODELS,
            RouteDependencyKey(RouteEvent.MESSAGE, "credit_cmds"): _MODELS,
            RouteDependencyKey(
                RouteEvent.CALLBACK_QUERY, "credit_purchase_intake"
            ): RouteDependencyPlan(
                models=True,
                commerce=frozenset({CommerceDependency.PURCHASE_INTENTS}),
            ),
            RouteDependencyKey(
                RouteEvent.PRE_CHECKOUT_QUERY, "credit_purchase_intake"
            ): RouteDependencyPlan(
                commerce=frozenset({CommerceDependency.PURCHASE_INTENTS}),
            ),
            RouteDependencyKey(
                RouteEvent.MESSAGE, "credit_payment_reconciliation"
            ): RouteDependencyPlan(
                commerce=frozenset({CommerceDependency.PAYMENT_SETTLEMENT}),
            ),
            RouteDependencyKey(RouteEvent.MESSAGE, "subscriptions"): (
                RouteDependencyPlan(
                    models=True,
                    commerce=frozenset({CommerceDependency.SUBSCRIPTION_MANAGEMENT}),
                )
            ),
            RouteDependencyKey(RouteEvent.CALLBACK_QUERY, "subscriptions"): (
                RouteDependencyPlan(
                    models=True,
                    commerce=frozenset({CommerceDependency.SUBSCRIPTION_MANAGEMENT}),
                )
            ),
            RouteDependencyKey(RouteEvent.MESSAGE, "image"): _MODELS,
            RouteDependencyKey(RouteEvent.MESSAGE, "tts"): _MODELS,
            RouteDependencyKey(RouteEvent.CALLBACK_QUERY, "tts"): _MODELS,
            RouteDependencyKey(
                RouteEvent.CHOSEN_INLINE_RESULT,
                "inline",
            ): _MODELS,
            RouteDependencyKey(RouteEvent.MESSAGE, "chat"): _MODELS,
            RouteDependencyKey(RouteEvent.CALLBACK_QUERY, "tool_approvals"): _MODELS,
        }
    )
)


class RouteDependencyMiddleware(BaseMiddleware):
    """Apply one immutable dependency plan after handler filters match."""

    def __init__(
        self,
        db: DatabaseManager,
        plans: Mapping[RouteDependencyKey, RouteDependencyPlan] = (
            ROUTE_DEPENDENCY_PLANS
        ),
    ) -> None:
        self._plans = dict(plans)
        self._models = DatabaseModelMiddleware(db)
        self._commerce = CommerceMiddleware(db)
        self._legacy_credit = CreditServiceMiddleware(db)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        event_name = _event_name(data)
        event_router = data.get("event_router")
        plan = (
            self._plans.get(RouteDependencyKey(event_name, event_router.name))
            if event_name is not None and isinstance(event_router, Router)
            else None
        )
        if plan is not None:
            if plan.models:
                await self._models.inject(data)
            if plan.commerce:
                self._commerce.inject(data, plan.commerce)
            if plan.legacy_credit:
                self._legacy_credit.inject(data)
        return await handler(event, data)


def setup_route_dependencies(
    dispatcher: Router,
    db: DatabaseManager,
) -> RouteDependencyMiddleware:
    """Register dependency loading on matched event handlers, not updates."""
    middleware = RouteDependencyMiddleware(db)
    for event in RouteEvent:
        dispatcher.observers[event.value].middleware(middleware)
    return middleware


def _event_name(data: Mapping[str, Any]) -> RouteEvent | None:
    update = data.get("event_update")
    if update is None:
        return None
    try:
        return RouteEvent(update.event_type)
    except ValueError:
        return None


__all__ = [
    "ROUTE_DEPENDENCY_PLANS",
    "RouteDependencyKey",
    "RouteDependencyMiddleware",
    "RouteDependencyPlan",
    "RouteEvent",
    "setup_route_dependencies",
]
