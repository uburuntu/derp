from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat, Message, User
from derp.memory.models import Note
from derp.money.wallets import balance, wallet
from derp.ops.import_main import NOTE_CHARS, Counts, import_main, note_pieces

pytestmark = pytest.mark.db

BOT_ID = 555000111
NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)

LEGACY = """
create table users (
    id uuid primary key, telegram_id bigint unique, is_bot boolean, first_name varchar, last_name varchar,
    username varchar, language_code varchar, is_premium boolean, credits integer
);
create table chats (
    id uuid primary key, telegram_id bigint unique, type varchar, title varchar, username varchar,
    is_forum boolean, llm_memory text, credits integer
);
create table messages (
    id uuid primary key, chat_id uuid references chats(id), user_id uuid references users(id),
    telegram_message_id bigint, thread_id bigint, direction varchar(3), content_type varchar, text text,
    media_group_id varchar, attachment_type varchar, attachment_file_id varchar, reply_to_message_id bigint,
    telegram_date timestamptz, edited_at timestamptz, deleted_at timestamptz
);
insert into users values
    ('00000000-0000-0000-0000-000000000001', 4001, false, 'Ann', null, 'ann', 'ru', false, 40),
    ('00000000-0000-0000-0000-000000000002', 4002, false, 'Bob', 'B', null, 'en', true, 0);
insert into chats values
    ('00000000-0000-0000-0000-00000000000a', -1004, 'supergroup', 'Old crew', null, false,
     'Ann loves cats' || chr(10) || 'Bob is vegan', 5),
    ('00000000-0000-0000-0000-00000000000b', 4001, 'private', null, 'ann', false, null, 0);
insert into messages values
    ('00000000-0000-0000-0000-0000000000a1', '00000000-0000-0000-0000-00000000000a',
     '00000000-0000-0000-0000-000000000001', 10, 7, 'in', 'text', 'derp hi', null, null, null, null,
     '2026-10-15 10:00+00', null, null),
    ('00000000-0000-0000-0000-0000000000a2', '00000000-0000-0000-0000-00000000000a', null, 11, null,
     'out', 'text', 'hello Ann', null, null, null, 10, '2026-10-15 10:01+00', null, null),
    ('00000000-0000-0000-0000-0000000000a3', '00000000-0000-0000-0000-00000000000a',
     '00000000-0000-0000-0000-000000000002', 12, null, 'in', 'photo', null, null, 'photo', 'AgAD', null,
     '2026-10-15 10:02+00', null, null),
    ('00000000-0000-0000-0000-0000000000a4', '00000000-0000-0000-0000-00000000000a',
     '00000000-0000-0000-0000-000000000002', 13, null, 'in', 'text', 'deleted', null, null, null, null,
     '2026-10-15 10:03+00', null, '2026-10-15 11:00+00'),
    ('00000000-0000-0000-0000-0000000000a5', '00000000-0000-0000-0000-00000000000b',
     '00000000-0000-0000-0000-000000000001', 3, null, 'in', 'text', 'too old', null, null, null, null,
     '2026-08-01 10:00+00', null, null);
"""


async def _import(database: Database, settings: Settings) -> Counts:
    async with database.engine.connect() as source:
        await source.execute(text("set search_path = legacy"))
        counts = await import_main(
            source, database, settings.credits_per_star, bot_id=BOT_ID, days=30, now=NOW
        )
        await source.rollback()
    return counts


def test_memory_splits_into_note_sized_pieces() -> None:
    assert list(note_pieces("a\n\n b \nc")) == ["a\nb\nc"]
    long = "x" * (NOTE_CHARS + 10)
    assert [len(p) for p in note_pieces(f"{long}\nshort")] == [
        NOTE_CHARS,
        16,
    ]  # the remainder and the next line share a note
    assert list(note_pieces("")) == []


async def test_imports_people_chats_balances_notes_and_recent_text(
    database: Database, settings: Settings
) -> None:
    async with database.engine.begin() as connection:
        await connection.execute(text("drop schema if exists legacy cascade"))
        await connection.execute(text("create schema legacy"))
        await connection.execute(text("set local search_path = legacy"))
        for statement in LEGACY.split(";"):
            if statement.strip():
                await connection.execute(text(statement))
    try:
        first, rerun = [await _import(database, settings) for _ in range(2)]
        assert (first.users, first.chats, first.lots, first.notes, first.messages) == (2, 2, 2, 1, 3)
        assert (rerun.users, rerun.chats, rerun.lots, rerun.notes, rerun.messages) == (0, 0, 0, 0, 0)
    finally:
        async with database.engine.begin() as connection:
            await connection.execute(text("drop schema legacy cascade"))

    async with database.session() as session:
        ann = await session.get(User, 4001)
        assert ann is not None and ann.language_code == "ru"
        crew = await session.get(Chat, -1004)
        assert crew is not None and crew.title == "Old crew" and crew.onboarded_at is None
        notes = list(await session.scalars(select(Note.text).where(Note.chat_id == -1004)))
        assert notes == ["Ann loves cats\nBob is vegan"]
        assert (
            await balance(session, (await wallet(session, "user", 4001)).id) == 40 * settings.credits_per_star
        )
        assert (
            await balance(session, (await wallet(session, "chat", -1004)).id) == 5 * settings.credits_per_star
        )
        rows = list(await session.scalars(select(Message).order_by(Message.chat_id, Message.message_id)))
    assert [(m.chat_id, m.message_id) for m in rows] == [(-1004, 10), (-1004, 11), (-1004, 12)]
    hi, answer, photo = rows
    assert hi.capture == "ambient" and hi.user_id == 4001 and hi.thread_id is None
    assert (
        answer.is_derp
        and answer.user_id == BOT_ID
        and answer.capture == "derp"
        and answer.reply_to_message_id == 10
    )
    assert photo.kind == "photo" and photo.text is None
    assert NOW - hi.date < timedelta(days=2)
