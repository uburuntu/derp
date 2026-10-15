"""Route invocations (not commands) to the agent runtime."""

from aiogram import Router
from aiogram.types import Message

from derp.agent.invocation import Trigger, TriggerKind
from derp.agent.runtime import Runtime
from derp.history.models import Chat


async def invoked(message: Message, trigger: Trigger | None = None) -> bool:
    return trigger is not None and trigger.kind is not TriggerKind.COMMAND


async def on_invocation(message: Message, chat_row: Chat, runtime: Runtime) -> None:
    await runtime.answer(message, chat_row)


def build_router() -> Router:
    router = Router(name="agent")
    router.message.register(on_invocation, invoked)
    return router
