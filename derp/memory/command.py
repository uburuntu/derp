"""/memory: what Derp remembers here, and approval of proposed chat notes."""

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from derp.db.engine import Database
from derp.history.models import Chat
from derp.locales import resolve, t
from derp.memory.models import Note
from derp.memory.notes import is_admin, load_memory
from derp.telegram.sender import Target, send_markdown


class NoteAction(CallbackData, prefix="note"):
    action: str  # approve | reject
    note_id: int


def _lines(notes: list[Note]) -> list[str]:
    return [f"- `#{n.id}` {n.text}" for n in notes]


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
            + ("\n".join(_lines(active)) or t(locale, "memory.empty"))
        )
    sections.append(
        t(locale, "memory.my_notes")
        + "\n"
        + ("\n".join(_lines(memory.my_notes)) or t(locale, "memory.empty"))
    )
    markup = None
    if proposed:
        sections.append(t(locale, "memory.proposed") + "\n" + "\n".join(_lines(proposed)))
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"✅ #{n.id}", callback_data=NoteAction(action="approve", note_id=n.id).pack()
                    ),
                    InlineKeyboardButton(
                        text=f"✖️ #{n.id}", callback_data=NoteAction(action="reject", note_id=n.id).pack()
                    ),
                ]
                for n in proposed[:10]
            ]
        )
    sections.append(t(locale, "memory.hint"))
    await send_markdown(
        bot, Target.reply_to_message(message), "\n\n".join(sections), trusted=False, reply_markup=markup
    )


async def on_note_action(query: CallbackQuery, callback_data: NoteAction, bot: Bot, db: Database) -> None:
    locale = resolve(query.from_user.language_code)
    message = query.message
    if message is None:
        await query.answer()
        return
    async with db.transaction() as session:
        chat = await session.get(Chat, message.chat.id)
        note = await session.get(Note, callback_data.note_id)
        if chat is None or note is None or note.chat_id != chat.id or note.state != "proposed":
            await query.answer(t(locale, "memory.gone"))
            return
        if not await is_admin(bot, chat, query.from_user.id):
            await query.answer(t(locale, "memory.admins_only"), show_alert=True)
            return
        if callback_data.action == "approve":
            note.state = "active"
            answer = t(locale, "memory.approved", id=note.id)
        else:
            await session.delete(note)
            answer = t(locale, "memory.rejected", id=note.id)
    await query.answer(answer)


def build_router() -> Router:
    router = Router(name="memory")
    router.message.register(show_memory, Command("memory"))
    router.callback_query.register(on_note_action, NoteAction.filter(F.action.in_({"approve", "reject"})))
    return router
