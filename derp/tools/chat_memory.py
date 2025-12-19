"""Chat memory management tool for Pydantic-AI agents.

This tool allows the AI agent to update the chat's persistent memory,
enabling personalized interactions across conversations.
"""

from __future__ import annotations

import logfire
from aiogram import html
from pydantic_ai import RunContext

from derp.db import update_chat_memory as db_update_chat_memory
from derp.llm.deps import AgentDeps


async def update_chat_memory(ctx: RunContext[AgentDeps], full_memory: str) -> str:
    """Save the entire memory state after combining existing memory with new facts.

    The memory has a 1024 character limit. Keep it concise and remove less
    important information if the limit would be exceeded.

    Args:
        ctx: The run context with agent dependencies.
        full_memory: The complete new memory state to save.

    Returns:
        A confirmation message with the new memory length.
    """
    # Validate memory length
    if len(full_memory) > 1024:
        return (
            f"Memory exceeds 1024 characters limit. "
            f"Current length is {len(full_memory)} characters. "
            f"Please provide a shorter memory state."
        )

    try:
        # Update memory in database using the db from deps
        async with ctx.deps.db.session() as session:
            await db_update_chat_memory(
                session,
                telegram_id=ctx.deps.chat_id,
                llm_memory=full_memory.strip(),
            )

        # Send system message to user about memory update
        await ctx.deps.message.reply(
            "(System message) Memory updated:\n"
            + html.expandable_blockquote(html.quote(full_memory.strip()))
        )

        logfire.info(
            "chat_memory_updated",
            chat_id=ctx.deps.chat_id,
            length=len(full_memory),
        )

        return f"Memory updated successfully. New memory length: {len(full_memory)} characters."

    except Exception:
        logfire.exception("chat_memory_update_failed", chat_id=ctx.deps.chat_id)
        return "Failed to update memory. Please try again."
