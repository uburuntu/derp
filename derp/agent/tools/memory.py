"""Memory tools: remember, find, edit and forget notes, set chat rules.

Who may change what is ``memory.notes.may_change`` (the chat's notes mode and
the asker's admin status, never tool arguments); the tools only word its
verdict. Not offered to free-model runs.
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
from derp.memory.notes import (
    FOUND_NOTES,
    RULES_CHARS,
    add_note,
    is_admin,
    load_memory,
    matching,
    may_change,
    rewrite,
)
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
        text: The fact in a sentence or two, or the whole list (max 500 characters).
        about: "chat" for a chat note, "me" for a note about the current person.
    """
    deps = ctx.deps
    owner = deps.user_id if about == "me" else None
    verdict = may_change("add", owner, deps.user_id, await _admin(deps), deps.chat.notes_mode)
    if verdict == "refused":
        return "Chat notes are turned off here; nothing saved."
    async with deps.db.transaction() as session:
        result = await add_note(
            session,
            deps.chat.id,
            text,
            user_id=owner,
            author_id=deps.user_id,
            state="proposed" if verdict == "propose" else "active",
        )
    if isinstance(result, str):
        return f"Not saved: {result}."
    if verdict == "propose":
        return f"Proposed as note {result.id}; an admin has to approve it in /memory."
    return f"Saved as note {result.id}."


@toolset.tool
async def find_notes(ctx: RunContext[RunDeps], queries: list[str] | None = None) -> str:
    """Search this chat's notes and the current person's own notes, older ones the situation left out too.

    Send a few short phrasings in one call, as for search_history: synonyms, the Russian and English
    versions. Any word may match (inflected forms too); notes matching more words rank higher.

    Args:
        queries: Keywords or short phrases, one phrasing each; none lists every note, newest first.
    """
    deps = ctx.deps
    async with deps.db.session() as session:
        memory = await load_memory(session, deps.chat.id, deps.user_id)
    found = matching(memory, (queries or [])[:5])
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
        text: The note's full new text (max 500 characters).
    """
    deps = ctx.deps
    admin = await _admin(deps)
    async with deps.db.transaction() as session:
        note = await _note(session, deps, note_id)
        if note is None:
            return NO_NOTE
        verdict = may_change("edit", note.user_id, deps.user_id, admin, deps.chat.notes_mode)
        if verdict == "refused":
            return _refusal(note, "change")
        result = await rewrite(session, note, text, propose=verdict == "propose", author_id=deps.user_id)
    if isinstance(result, str):
        return f"Not changed: {result}."
    if verdict == "propose":
        return f"Proposed the change to note {note_id}; an admin has to approve it in /memory."
    return f"Note {note_id} updated."


@toolset.tool
async def forget_note(ctx: RunContext[RunDeps], note_id: int) -> str:
    """Delete a saved note by its number (chat notes or the current person's own notes).

    Args:
        note_id: The number of "note 42".
    """
    deps = ctx.deps
    admin = await _admin(deps)
    async with deps.db.transaction() as session:
        note = await _note(session, deps, note_id)
        if note is None:
            return NO_NOTE
        if may_change("forget", note.user_id, deps.user_id, admin, deps.chat.notes_mode) == "refused":
            return _refusal(note, "remove")
        await session.delete(note)  # a pending edit of it goes too
    return f"Forgot note {note_id}."


NO_NOTE = "No such note in this chat."


async def _admin(deps: RunDeps) -> bool:
    return await is_admin(deps.bot, deps.chat, deps.user_id)


async def _note(session: AsyncSession, deps: RunDeps, note_id: int) -> Note | None:
    """One of this chat's active notes."""
    note = await session.get(Note, note_id)
    return note if note is not None and note.chat_id == deps.chat.id and note.state == "active" else None


def _refusal(note: Note, verb: str) -> str:
    if note.user_id is not None:
        return f"That is someone else's personal note; only they can {verb} it."
    return f"Only admins can {verb} chat notes here."


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
