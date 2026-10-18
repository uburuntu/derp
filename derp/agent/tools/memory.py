"""Memory tools: remember, find, edit and forget notes, set chat rules.

Permissions come from the chat's notes mode and the asker's admin status —
never from tool arguments. Not offered to free-model runs.
"""

from typing import Literal

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset
from sqlalchemy.ext.asyncio import AsyncSession

from derp.agent.deps import RunDeps
from derp.agent.outputs import Refused, check_budget, deliver
from derp.agent.toolsets import Family
from derp.history.models import Chat, Message
from derp.history.render import neutralize
from derp.memory.command import rules_keyboard, rules_prompt
from derp.memory.models import Note
from derp.memory.notes import FOUND_NOTES, RULES_CHARS, add_note, is_admin, load_memory, matching, rewrite
from derp.money.funding import Funding
from derp.telegram.sender import send_markdown

toolset = FunctionToolset[RunDeps]()


@toolset.tool
async def remember(ctx: RunContext[RunDeps], text: str, about: Literal["chat", "me"] = "chat") -> str:
    """Save a short fact for future conversations in this chat.

    Use "me" for facts about the person talking to you (their preferences, plans), "chat" for things
    the whole chat should know. Chat notes follow the chat notes policy in the situation; personal
    notes are saved when asked, or when something about the person is clearly worth keeping.
    A correction of a saved fact is edit_note, not a second note. Keep a list (films to watch, places,
    games) as one note that you edit to add or strike items, not as many tiny notes.

    Args:
        text: The fact, one short sentence (max 300 characters).
        about: "chat" for a chat note, "me" for a note about the current person.
    """
    deps = ctx.deps
    chat = deps.chat
    state = "active"
    if about == "chat":
        if chat.notes_mode == "off":
            return "Chat notes are turned off here; nothing saved."
        if chat.notes_mode == "admins" and not await is_admin(deps.bot, chat, deps.user_id):
            state = "proposed"
    async with deps.db.transaction() as session:
        result = await add_note(
            session,
            chat.id,
            text,
            user_id=deps.user_id if about == "me" else None,
            author_id=deps.user_id,
            state=state,
        )
    if isinstance(result, str):
        return f"Not saved: {result}."
    if state == "proposed":
        return f"Proposed as note {result.id}; an admin has to approve it in /memory."
    return f"Saved as note {result.id}."


@toolset.tool
async def find_notes(ctx: RunContext[RunDeps], query: str = "") -> str:
    """Search this chat's notes and the current person's own notes, older ones the situation left out too.

    Args:
        query: Words to look for (parts of words match too); empty lists every note, newest first.
    """
    deps = ctx.deps
    async with deps.db.session() as session:
        memory = await load_memory(session, deps.chat.id, deps.user_id)
    found = matching(memory, query)
    if not found:
        return "No notes match."
    lines = [
        f"- note {n.id} ({'chat' if n.user_id is None else 'about them'}): {neutralize(n.text)}"
        for n in found[:FOUND_NOTES]
    ]
    if len(found) > FOUND_NOTES:
        lines.append(f"{len(found) - FOUND_NOTES} more match; search with more specific words.")
    return "\n".join(lines)


@toolset.tool
async def edit_note(ctx: RunContext[RunDeps], note_id: int, text: str) -> str:
    """Rewrite a saved note in place: corrections ("actually, it's Thursdays") and lists (add or strike an
    item). Chat notes follow the chat notes policy; a personal note is only its owner's to change.

    Args:
        note_id: The number of "note 42".
        text: The note's full new text (max 300 characters).
    """
    deps = ctx.deps
    async with deps.db.transaction() as session:
        found = await _changeable(session, deps, note_id)
        if isinstance(found, str):
            return found
        note, direct = found
        if not direct and deps.chat.notes_mode != "admins":
            return "Only admins can change chat notes here."
        result = await rewrite(session, note, text, propose=not direct, author_id=deps.user_id)
    if isinstance(result, str):
        return f"Not changed: {result}."
    if not direct:
        return f"Proposed the change to note {note_id}; an admin has to approve it in /memory."
    return f"Note {note_id} updated."


@toolset.tool
async def forget_note(ctx: RunContext[RunDeps], note_id: int) -> str:
    """Delete a saved note by its number (chat notes or the current person's own notes).

    Args:
        note_id: The number of "note 42".
    """
    deps = ctx.deps
    async with deps.db.transaction() as session:
        found = await _changeable(session, deps, note_id)
        if isinstance(found, str):
            return found
        note, direct = found
        if not direct:
            return "Only admins can remove chat notes here."
        await session.delete(note)
    return f"Forgot note {note_id}."


async def _changeable(session: AsyncSession, deps: RunDeps, note_id: int) -> tuple[Note, bool] | str:
    """The run's note and whether the asker may change it directly (chat notes in admins and off modes:
    admins only), or why it isn't theirs to change."""
    note = await session.get(Note, note_id)
    if note is None or note.chat_id != deps.chat.id or note.state != "active":
        return "No such note in this chat."
    if note.user_id is not None:
        if note.user_id != deps.user_id:
            return "That is someone else's personal note; only they can change it."
        return note, True
    if deps.chat.notes_mode in {"admins", "off"}:
        return note, await is_admin(deps.bot, deps.chat, deps.user_id)
    return note, True


@toolset.tool(sequential=True)
async def set_chat_rules(ctx: RunContext[RunDeps], rules: str) -> str:
    """Replace this chat's rules — standing instructions for how you behave here. Admins only.

    Rules quoted word for word from the admin's current message apply at once; anything you reworded
    (or clearing the rules) is shown to the admin with Apply/Cancel buttons first.

    Args:
        rules: The full new rules text (max 2000 characters); empty clears them.
    """
    deps = ctx.deps
    if not await is_admin(deps.bot, deps.chat, deps.user_id):
        return "Only chat admins can change the rules."
    text = rules.strip()
    if len(text) > RULES_CHARS:
        return f"Too long: rules are limited to {RULES_CHARS} characters."
    async with deps.db.session() as session:
        trigger = await session.get(Message, (deps.chat.id, deps.trigger_message_id))
    # Rules are the one trusted chat text, so the model may not word them itself unconfirmed:
    # injected chat text could otherwise become standing instructions.
    if text and trigger is not None and _plain(text) in _plain(trigger.text or ""):
        async with deps.db.transaction() as session:
            chat = await session.get(Chat, deps.chat.id)
            if chat is None:
                return "This chat isn't known yet."
            chat.rules, chat.rules_draft, chat.rules_draft_by = text, None, None
        deps.chat.rules = text
        return "Rules updated; they apply from the next message."
    try:
        check_budget(deps)
    except Refused:
        return "Output limit for this turn reached; ask the admin to send the exact rules text instead."
    async with deps.db.transaction() as session:
        chat = await session.get(Chat, deps.chat.id)
        if chat is None:
            return "This chat isn't known yet."
        chat.rules_draft, chat.rules_draft_by = text, deps.user_id
    try:
        await deliver(
            deps,
            "other",
            "rules card",
            lambda: send_markdown(
                deps.bot,
                deps.target,
                rules_prompt(deps.locale, text),
                trusted=False,
                reply_markup=rules_keyboard(deps.locale, text),
            ),
            history=False,
        )
    except Refused as refused:
        return str(refused)
    return "Shown to the admin with Apply/Cancel buttons; nothing changes until they press Apply."


def _plain(text: str) -> str:
    return " ".join(text.casefold().split())


family = Family(
    "memory", toolset, tiers=frozenset({Funding.PAID, Funding.SPONSORED}), deferred=("set_chat_rules",)
)
