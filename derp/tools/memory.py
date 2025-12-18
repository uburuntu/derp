"""Tool for updating chat memory."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import logfire
from aiogram import html
from aiogram.types import Message

from derp.db import update_chat_memory
from derp.models import Chat as ChatModel


@dataclass(frozen=True, slots=True)
class ToolDeps:
    """Dependencies passed to tool functions."""

    message: Message
    chat_settings: ChatModel | None = None
    db_client: Any | None = None


async def update_memory(full_memory: str, deps: ToolDeps) -> str:
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
            logfire.warning("memory_update_no_db", chat_id=deps.message.chat.id)
            return "Database not available for memory storage"

        async with deps.db_client.session() as session:
            await update_chat_memory(
                session,
                telegram_id=deps.message.chat.id,
                llm_memory=full_memory.strip(),
            )

        # Send system message to user about memory update
        await deps.message.reply(
            "(System message) Memory updated:\n"
            + html.expandable_blockquote(html.quote(full_memory.strip()))
        )

        logfire.info(
            "memory_updated",
            chat_id=deps.message.chat.id,
            length=len(full_memory),
        )
        return f"Memory updated successfully. New memory length: {len(full_memory)} characters."

    except Exception:
        logfire.exception("memory_update_failed", chat_id=deps.message.chat.id)
        return "Failed to store memory"
