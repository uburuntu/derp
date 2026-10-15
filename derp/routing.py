"""Ordered router registration. First match wins, so order is behavior.

Feature modules expose ``build_router()`` (a fresh Router per dispatcher)
instead of module-level routers, which aiogram can attach only once.
"""

from aiogram import Dispatcher


def include_routers(dispatcher: Dispatcher) -> None:
    from derp.agent import handler
    from derp.history import members, polls, reactions
    from derp.memory import command as memory
    from derp.settings import onboarding, start

    dispatcher.include_routers(
        members.build_router(),
        onboarding.build_router(),
        reactions.build_router(),
        polls.build_router(),
        start.build_router(),
        memory.build_router(),
        handler.build_router(),  # last: invocations that no command claimed
    )
