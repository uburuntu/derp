"""Ordered router registration. First match wins, so order is behavior.

Feature modules expose ``build_router()`` (a fresh Router per dispatcher)
instead of module-level routers, which aiogram can attach only once.
"""

from aiogram import Dispatcher


def include_routers(dispatcher: Dispatcher) -> None:
    from derp.agent import approvals, handler, inline
    from derp.billing import command as billing
    from derp.history import members, polls, reactions
    from derp.memory import command as memory
    from derp.money import command as money
    from derp.money import free
    from derp.settings import onboarding, pages, privacy, start

    dispatcher.include_routers(
        members.build_router(),
        onboarding.build_router(),
        reactions.build_router(),
        polls.build_router(),
        start.build_router(),
        pages.build_router(),
        privacy.build_router(),
        memory.build_router(),
        money.build_router(),
        free.build_router(),
        approvals.build_router(),
        inline.build_router(),
        billing.build_router(),  # before the agent: payment service messages invoke nothing
        handler.build_router(),  # last: invocations that no command claimed
    )
