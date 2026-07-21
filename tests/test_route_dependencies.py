"""Integration tests for matched-route dependency loading."""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    PreCheckoutQuery,
    Update,
    User,
)

from derp.application import APPLICATION_ROUTERS
from derp.billing import PaymentSettlementService, PurchaseIntentService
from derp.middlewares.commerce import CommerceDependency
from derp.middlewares.database_logger import DatabaseLoggerMiddleware
from derp.middlewares.route_dependencies import (
    ROUTE_DEPENDENCY_PLANS,
    RouteDependencyKey,
    RouteEvent,
    setup_route_dependencies,
)

_DOMAIN_DEPENDENCIES = {
    "user_model",
    "chat_model",
    "credit_service",
    "purchase_intents",
    "payment_settlement",
    "subscription_management",
}


@pytest.fixture
def route_db() -> MagicMock:
    db = MagicMock()
    session = AsyncMock()
    db.read_session.return_value.__aenter__.return_value = session
    db.read_session.return_value.__aexit__.return_value = None
    return db


@pytest.fixture
async def route_bot():
    bot = Bot("123456:route_dependency_test")
    yield bot
    await bot.session.close()


def _user() -> User:
    return User(id=42, is_bot=False, first_name="Ada")


def _message(*, text: str = "hello") -> Message:
    return Message(
        message_id=7,
        date=datetime.now(UTC),
        chat=Chat(id=42, type="private", first_name="Ada"),
        from_user=_user(),
        text=text,
    )


def _message_update(*, text: str = "hello") -> Update:
    return Update(update_id=1, message=_message(text=text))


def _callback_update() -> Update:
    return Update(
        update_id=2,
        callback_query=CallbackQuery(
            id="callback-1",
            from_user=_user(),
            chat_instance="route-test",
            message=_message(),
            data="test",
        ),
    )


def _pre_checkout_update() -> Update:
    return Update(
        update_id=3,
        pre_checkout_query=PreCheckoutQuery(
            id="checkout-1",
            from_user=_user(),
            currency="XTR",
            total_amount=1,
            invoice_payload="opaque",
        ),
    )


def _domain_snapshot(data: dict[str, object]) -> dict[str, object]:
    return {key: data[key] for key in _DOMAIN_DEPENDENCIES if key in data}


def test_route_plans_cover_every_declared_dynamic_handler_dependency() -> None:
    """Keep new handlers from silently depending on a global middleware path."""

    def walk(router: Router) -> Iterator[Router]:
        yield router
        for child in router.sub_routers:
            yield from walk(child)

    for root in APPLICATION_ROUTERS:
        for router in walk(root):
            for event_name, observer in router.observers.items():
                try:
                    route_event = RouteEvent(event_name)
                except ValueError:
                    continue
                requested = {
                    dependency
                    for handler in observer.handlers
                    for dependency in inspect.signature(handler.callback).parameters
                    if dependency in _DOMAIN_DEPENDENCIES
                }
                if not requested:
                    continue
                plan = ROUTE_DEPENDENCY_PLANS[
                    RouteDependencyKey(route_event, router.name)
                ]
                provided = {dependency.value for dependency in plan.commerce}
                if plan.models:
                    provided.update({"user_model", "chat_model"})
                if plan.legacy_credit:
                    provided.add("credit_service")
                assert requested <= provided

    chat_plan = ROUTE_DEPENDENCY_PLANS[RouteDependencyKey(RouteEvent.MESSAGE, "chat")]
    assert chat_plan.models
    assert not chat_plan.legacy_credit
    assert CommerceDependency.PAYMENT_SETTLEMENT not in chat_plan.commerce
    for event in (RouteEvent.MESSAGE, RouteEvent.CALLBACK_QUERY):
        tts_plan = ROUTE_DEPENDENCY_PLANS[RouteDependencyKey(event, "tts")]
        assert tts_plan.models
        assert not tts_plan.legacy_credit


@pytest.mark.asyncio
async def test_pre_checkout_gets_only_purchase_intents(
    route_db: MagicMock,
    route_bot: Bot,
) -> None:
    dispatcher = Dispatcher()
    setup_route_dependencies(dispatcher, route_db)
    router = Router(name="credit_purchase_intake")
    observed: dict[str, object] = {}

    @router.pre_checkout_query()
    async def capture(_query: PreCheckoutQuery, **data: object) -> None:
        observed.update(_domain_snapshot(data))

    parent = Router(name="payments")
    parent.include_router(router)
    dispatcher.include_router(parent)
    await dispatcher.feed_update(route_bot, _pre_checkout_update())

    assert set(observed) == {"purchase_intents"}
    assert isinstance(observed["purchase_intents"], PurchaseIntentService)
    route_db.read_session.assert_not_called()
    route_db.session.assert_not_called()


@pytest.mark.asyncio
async def test_payment_reconciliation_gets_only_settlement_service(
    route_db: MagicMock,
    route_bot: Bot,
) -> None:
    dispatcher = Dispatcher()
    setup_route_dependencies(dispatcher, route_db)
    router = Router(name="credit_payment_reconciliation")
    observed: dict[str, object] = {}

    @router.message()
    async def capture(_message: Message, **data: object) -> None:
        observed.update(_domain_snapshot(data))

    parent = Router(name="payments")
    parent.include_router(router)
    dispatcher.include_router(parent)
    await dispatcher.feed_update(route_bot, _message_update())

    assert set(observed) == {"payment_settlement"}
    assert isinstance(observed["payment_settlement"], PaymentSettlementService)
    route_db.read_session.assert_not_called()
    route_db.session.assert_not_called()


@pytest.mark.asyncio
async def test_chat_gets_models_and_static_service_after_match(
    route_db: MagicMock,
    route_bot: Bot,
) -> None:
    chat_accounting = object()
    dispatcher = Dispatcher(chat_accounting=chat_accounting)
    setup_route_dependencies(dispatcher, route_db)
    router = Router(name="chat")
    observed: dict[str, object] = {}
    user_model = object()
    chat_model = object()

    @router.message()
    async def capture(_message: Message, **data: object) -> None:
        observed.update(_domain_snapshot(data))
        observed["chat_accounting"] = data["chat_accounting"]

    dispatcher.include_router(router)
    with (
        patch(
            "derp.middlewares.db_models.get_user_by_telegram_id",
            new=AsyncMock(return_value=user_model),
        ),
        patch(
            "derp.middlewares.db_models.get_chat_settings",
            new=AsyncMock(return_value=chat_model),
        ),
    ):
        await dispatcher.feed_update(route_bot, _message_update())

    assert observed["user_model"] is user_model
    assert observed["chat_model"] is chat_model
    assert observed["chat_accounting"] is chat_accounting
    assert set(observed) == {"user_model", "chat_model", "chat_accounting"}
    route_db.read_session.assert_called_once()
    route_db.session.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("router_name", ["context_settings", "tool_approvals"])
async def test_settings_and_approval_callbacks_get_models_only(
    router_name: str,
    route_db: MagicMock,
    route_bot: Bot,
) -> None:
    dispatcher = Dispatcher()
    setup_route_dependencies(dispatcher, route_db)
    router = Router(name=router_name)
    observed: dict[str, object] = {}
    user_model = object()
    chat_model = object()

    @router.callback_query()
    async def capture(_callback: CallbackQuery, **data: object) -> None:
        observed.update(_domain_snapshot(data))

    if router_name == "tool_approvals":
        parent = Router(name="chat")
        parent.include_router(router)
        dispatcher.include_router(parent)
    else:
        dispatcher.include_router(router)
    with (
        patch(
            "derp.middlewares.db_models.get_user_by_telegram_id",
            new=AsyncMock(return_value=user_model),
        ),
        patch(
            "derp.middlewares.db_models.get_chat_settings",
            new=AsyncMock(return_value=chat_model),
        ),
    ):
        await dispatcher.feed_update(route_bot, _callback_update())

    assert observed == {"user_model": user_model, "chat_model": chat_model}
    route_db.read_session.assert_called_once()
    route_db.session.assert_not_called()


@pytest.mark.asyncio
async def test_unmatched_message_does_not_load_route_dependencies(
    route_db: MagicMock,
    route_bot: Bot,
) -> None:
    dispatcher = Dispatcher()
    setup_route_dependencies(dispatcher, route_db)
    router = Router(name="chat")

    @router.message(lambda message: message.text == "matched")
    async def capture(_message: Message) -> None:
        raise AssertionError("handler should not match")

    dispatcher.include_router(router)
    result = await dispatcher.feed_update(
        route_bot,
        _message_update(text="unmatched"),
    )

    assert result is UNHANDLED
    route_db.read_session.assert_not_called()
    route_db.session.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_update_skips_persistence_and_route_queries(
    route_db: MagicMock,
    route_bot: Bot,
) -> None:
    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(
        DatabaseLoggerMiddleware(
            route_db,
            bot_id=123456,
            bot_username="derp_test",
        )
    )
    setup_route_dependencies(dispatcher, route_db)

    with pytest.warns(RuntimeWarning, match="unknown update type"):
        result = await dispatcher.feed_update(route_bot, Update(update_id=4))

    assert result is UNHANDLED
    route_db.read_session.assert_not_called()
    route_db.session.assert_not_called()
