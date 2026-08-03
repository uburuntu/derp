"""Agent adapter for proposing topic-scoped facts with native admin review."""

from __future__ import annotations

from enum import StrEnum

from aiogram import html
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.i18n import gettext as _
from pydantic_ai import RunContext, Tool

from derp.db import propose_shared_fact as db_propose_shared_fact
from derp.history.capture import suppress_outbound_history
from derp.llm.deps import AgentDeps
from derp.tools.policy import ChatTool

_SENT_DIRECTLY = (
    "[Sent a shared-fact proposal directly to the chat for review. Do not repeat it.]"
)


class SharedFactAction(StrEnum):
    """Authenticated server-side review actions."""

    APPROVE = "approve"
    REJECT = "reject"


class SharedFactCallback(CallbackData, prefix="fact"):
    """Compact callback coordinates for one scoped proposal."""

    action: SharedFactAction
    fact_id: str


async def propose_shared_fact(ctx: RunContext[AgentDeps], fact_text: str) -> str:
    """Propose a factual chat memory item for explicit human review.

    Args:
        fact_text: One concise fact, never an instruction or policy statement.
    """
    deps = ctx.deps
    if deps.chat_model is None or deps.user_model is None:
        return "Shared facts are unavailable for this conversation."
    async with deps.db.session() as session:
        proposal = await db_propose_shared_fact(
            session,
            chat_id=deps.chat_model.id,
            thread_id=deps.message.message_thread_id,
            proposer_user_id=deps.user_model.id,
            fact_text=fact_text,
        )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=_("Approve"),
                    callback_data=SharedFactCallback(
                        action=SharedFactAction.APPROVE,
                        fact_id=str(proposal.id),
                    ).pack(),
                ),
                InlineKeyboardButton(
                    text=_("Reject"),
                    callback_data=SharedFactCallback(
                        action=SharedFactAction.REJECT,
                        fact_id=str(proposal.id),
                    ).pack(),
                ),
            ]
        ]
    )
    with suppress_outbound_history():
        title = html.quote(_("Save this fact?"))
        review_note = html.quote(_("An admin must approve it before Derp can use it."))
        await deps.message.reply(
            f"<b>{title}</b>\n"
            f"<blockquote>{html.quote(proposal.fact_text)}</blockquote>\n"
            f"{review_note}",
            reply_markup=markup,
        )
    return _SENT_DIRECTLY


class SharedFactTools:
    """Provide implemented shared-fact capabilities to the governed toolset."""

    def __init__(self) -> None:
        self._proposal = Tool[AgentDeps](propose_shared_fact)

    def get_tool(self, capability: ChatTool) -> Tool[AgentDeps] | None:
        """Return the proposal adapter; reviews stay human callback actions."""
        if capability is ChatTool.PROPOSE_SHARED_FACT:
            return self._proposal
        return None


__all__ = [
    "SharedFactAction",
    "SharedFactCallback",
    "SharedFactTools",
    "propose_shared_fact",
]
