"""Route invocations (not commands) to the agent runtime."""

from aiogram import Bot, Router
from aiogram.types import Message, MessageGenerationStopped

from derp.agent.invocation import Trigger, TriggerKind
from derp.agent.runtime import Runtime
from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat
from derp.settings.onboarding import GROUP_TYPES, ensure_onboarded


async def invoked(message: Message, trigger: Trigger | None = None) -> bool:
    return trigger is not None and trigger.kind is not TriggerKind.COMMAND


async def on_invocation(
    message: Message,
    chat_row: Chat,
    runtime: Runtime,
    bot: Bot,
    db: Database,
    settings: Settings,
) -> None:
    if chat_row.onboarded_at is None and chat_row.type in GROUP_TYPES:  # chats that predate the join notice
        await ensure_onboarded(
            bot, db, settings, chat_row.id, message.from_user.language_code if message.from_user else None
        )
    await runtime.answer(message, chat_row)


async def on_stop(update: MessageGenerationStopped, runtime: Runtime) -> None:
    runtime.stop(update.chat.id, update.draft_id)


def build_router() -> Router:
    router = Router(name="agent")
    router.message.register(on_invocation, invoked)
    router.stopped_message_generation.register(on_stop)
    return router
