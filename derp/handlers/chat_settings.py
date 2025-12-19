"""Handlers for chat settings management."""

from aiogram import Router, html
from aiogram.filters import Command
from aiogram.types import Message

from derp.db import DatabaseManager, update_chat_memory
from derp.models import Chat as ChatModel

router = Router(name="chat_model")


@router.message(Command("settings"))
async def cmd_show_settings(
    message: Message,
    chat_model: ChatModel | None,
) -> None:
    """Show current chat settings."""
    settings_text = "📋 Chat Settings:\n\n"

    if chat_model and chat_model.llm_memory:
        settings_text += (
            f"🧠 LLM Memory: {html.blockquote(html.quote(chat_model.llm_memory))}\n"
        )
    else:
        settings_text += "🧠 LLM Memory: Not set\n"

    return await message.reply(settings_text)


@router.message(Command("set_memory"))
async def cmd_set_memory(
    message: Message,
    db: DatabaseManager,
) -> None:
    """Set LLM memory for the chat."""
    # Extract memory text from command
    if not message.text:
        await message.answer("Usage: /set_memory <memory_text>")
        return

    command_args = message.text.split(maxsplit=1)
    if len(command_args) < 2:
        await message.answer(
            "Usage: /set_memory <memory_text>\n"
            "Example: /set_memory This chat is about Python programming"
        )
        return

    memory_text = command_args[1].strip()

    try:
        # Validate length
        if len(memory_text) > 1024:
            await message.answer("❌ Memory text cannot exceed 1024 characters.")
            return

        # Update in database
        async with db.session() as session:
            await update_chat_memory(
                session, telegram_id=message.chat.id, llm_memory=memory_text
            )

        await message.answer(f"✅ LLM memory updated:\n\n{memory_text}")

    except Exception as e:
        await message.answer(f"❌ Failed to update memory: {e!s}")


@router.message(Command("clear_memory"))
async def cmd_clear_memory(
    message: Message,
    db: DatabaseManager,
) -> None:
    """Clear LLM memory for the chat."""
    try:
        async with db.session() as session:
            await update_chat_memory(
                session, telegram_id=message.chat.id, llm_memory=None
            )

        await message.answer("✅ LLM memory cleared.")

    except Exception as e:
        await message.answer(f"❌ Failed to clear memory: {e!s}")
