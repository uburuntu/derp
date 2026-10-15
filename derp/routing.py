"""Ordered router registration. First match wins, so order is behavior.

Feature modules expose ``build_router()`` (a fresh Router per dispatcher)
instead of module-level routers, which aiogram can attach only once.
"""

from aiogram import Dispatcher


def include_routers(dispatcher: Dispatcher) -> None:
    from derp.settings import start

    dispatcher.include_routers(start.build_router())
