"""Tool for updating chat memory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import logfire
from aiogram import html
from aiogram.types import Message

from ..queries.chat_settings_async_edgeql import ChatSettingsResult
from ..queries.update_chat_settings_async_edgeql import update_chat_settings


@dataclass(frozen=True, slots=True)
class ToolDeps:
    """Dependencies passed to tool functions."""

    message: Message
    chat_settings: ChatSettingsResult | None = None
    db_client: Any | None = None


async def update_chat_memory(full_memory: str, deps: ToolDeps) -> str:
    """Save the entire memory state after combining existing memory with new facts.

    The memory has a 1024 character limit. Keep it concise and remove less important
    information if the limit would be exceeded.
    """
    try:
        # Validate memory length
        if len(full_memory) > 1024:
            return (
                f"Memory exceeds 1024 characters limit. "
                f"Current length is {len(full_memory)} characters. "
                f"Please provide a shorter memory state."
            )

        # Update memory in database
        if not deps.db_client:
            logfire.warning("No database client available for memory update")
            return "Database not available for memory storage"

        async with deps.db_client.get_executor() as executor:
            await update_chat_settings(
                executor,
                chat_id=deps.message.chat.id,
                llm_memory=full_memory.strip(),
            )

        # Send system message to user about memory update
        await deps.message.reply(
            "(System message) Memory updated:\n"
            + html.expandable_blockquote(html.quote(full_memory.strip()))
        )

        logfire.info(
            f"Memory updated for chat {deps.message.chat.id}, length: {len(full_memory)}"
        )
        return f"Memory updated successfully. New memory length: {len(full_memory)} characters."

    except Exception as e:
        logfire.warning(f"Failed to update chat memory: {e}")
        return f"Failed to store memory: {str(e)}"
