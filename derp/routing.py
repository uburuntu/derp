"""Ordered router registration. First match wins, so order is behavior."""

from aiogram import Dispatcher


def include_routers(dispatcher: Dispatcher) -> None:
    from derp.settings.start import router as start_router

    dispatcher.include_routers(start_router)
