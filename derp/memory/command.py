"""/memory: what Derp remembers here, approval of proposed chat notes, confirmation of chat rules."""

import hashlib

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, Message

from derp.db.engine import Database
from derp.history.models import Chat
from derp.locales import resolve, t
from derp.memory.models import Note
from derp.memory.notes import decide, is_admin, load_memory
from derp.telegram.buttons import Press, button, markup, on_press
from derp.telegram.sender import Target, send_markdown


class NoteAction(CallbackData, prefix="note"):
    action: str  # approve | reject
    note_id: int


class RulesAction(CallbackData, prefix="rules"):
    action: str  # apply | cancel
    draft: str  # digest of the draft the buttons were shown for


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:10]


def rules_prompt(locale: str, rules: str) -> str:
    if not rules:
        return t(locale, "memory.rules_clear")
    return t(locale, "memory.rules_confirm") + "\n\n" + rules


def rules_keyboard(locale: str, rules: str) -> InlineKeyboardMarkup | None:
    draft = _digest(rules)
    return markup(
        [
            button(
                t(locale, "memory.rules_apply"), RulesAction(action="apply", draft=draft), style="success"
            ),
            button(
                t(locale, "memory.rules_cancel"), RulesAction(action="cancel", draft=draft), style="danger"
            ),
        ]
    )


def _decided(note_id: int) -> set[str]:
    return {NoteAction(action=action, note_id=note_id).pack() for action in ("approve", "reject")}


def _label(locale: str, note: Note) -> str:
    """How people see a note's number, like the model does: "note 12"; a proposed edit goes by its note's."""
    if note.replaces_id is not None:
        return t(locale, "memory.note_edit", id=note.replaces_id)
    return t(locale, "memory.note", id=note.id)


def _lines(locale: str, notes: list[Note]) -> list[str]:
    return [f"- {_label(locale, n)}: {n.text}" for n in notes]


async def show_memory(message: Message, bot: Bot, db: Database, chat_row: Chat) -> None:
    if message.from_user is None:
        return
    locale = resolve(message.from_user.language_code)
    async with db.session() as session:
        memory = await load_memory(session, chat_row.id, message.from_user.id, include_proposed=True)
    active = [n for n in memory.chat_notes if n.state == "active"]
    proposed = [n for n in memory.chat_notes if n.state == "proposed"]
    sections = [t(locale, "memory.title")]
    if chat_row.rules:
        sections.append(t(locale, "memory.rules") + "\n" + chat_row.rules)
    if chat_row.type != "private":
        sections.append(
            t(locale, "memory.chat_notes", mode=t(locale, f"memory.mode.{chat_row.notes_mode}"))
            + "\n"
            + ("\n".join(_lines(locale, active)) or t(locale, "memory.empty"))
        )
    if chat_row.type == "private" or message.ephemeral_message_id:
        sections.append(
            t(locale, "memory.my_notes")
            + "\n"
            + ("\n".join(_lines(locale, memory.my_notes)) or t(locale, "memory.empty"))
        )
    else:  # a public reply: someone's own notes stay out of the group
        sections.append(t(locale, "memory.my_notes_hidden"))
    keyboard = None
    if proposed:
        sections.append(t(locale, "memory.proposed") + "\n" + "\n".join(_lines(locale, proposed)))
        keyboard = markup(
            *(
                [
                    button(f"✅ {_label(locale, n)}", NoteAction(action="approve", note_id=n.id)),
                    button(f"✖️ {_label(locale, n)}", NoteAction(action="reject", note_id=n.id)),
                ]
                for n in proposed[:10]
            )
        )
    sections.append(t(locale, "memory.hint"))
    await send_markdown(
        bot, Target.reply_to_message(message), "\n\n".join(sections), trusted=False, reply_markup=keyboard
    )


async def on_note_action(press: Press, data: NoteAction, bot: Bot, db: Database) -> None:
    """An admin's decision on one proposal (a new note, or an edit that rewrites its note); that proposal's
    buttons go, the others' stay."""
    refusal: str | None = None
    shown_id = data.note_id
    async with db.transaction() as session:
        chat = await session.get(Chat, press.chat_id)
        note = await session.get(Note, data.note_id)
        if chat is None or note is None or note.chat_id != chat.id or note.state != "proposed":
            refusal = "memory.gone"
        elif not await is_admin(bot, chat, press.user_id):
            return await press.deny("memory.admins_only")
        else:
            shown_id = note.replaces_id or note.id
            await decide(session, note, approve=data.action == "approve")
    decided = _decided(data.note_id)
    await press.strip(keep=lambda b: b.callback_data not in decided)
    if refusal:
        return await press.deny(refusal)
    await press.toast("memory.approved" if data.action == "approve" else "memory.rejected", id=shown_id)


async def on_rules_action(press: Press, data: RulesAction, bot: Bot, db: Database) -> None:
    """Only the admin who asked can apply or cancel, and only the draft these buttons showed."""
    async with db.session() as session:
        chat = await session.get(Chat, press.chat_id)
    if chat is None or chat.rules_draft is None or _digest(chat.rules_draft) != data.draft:
        await press.strip()
        return await press.deny("memory.gone")
    if chat.rules_draft_by != press.user_id or not await is_admin(bot, chat, press.user_id):
        return await press.deny("memory.rules_not_yours")
    async with db.transaction() as session:
        row = await session.get(Chat, chat.id, with_for_update=True)
        current = row is not None and row.rules_draft is not None and _digest(row.rules_draft) == data.draft
        if row is not None and current:
            if data.action == "apply":
                row.rules = row.rules_draft or None
            row.rules_draft, row.rules_draft_by = None, None
    await press.strip()
    if not current:
        return await press.deny("memory.gone")
    await press.toast("memory.rules_applied" if data.action == "apply" else "memory.rules_cancelled")


def build_router() -> Router:
    router = Router(name="memory")
    on_press(router, RulesAction, F.action.in_({"apply", "cancel"}))(on_rules_action)
    router.message.register(show_memory, Command("memory"))
    on_press(router, NoteAction, F.action.in_({"approve", "reject"}))(on_note_action)
    return router
