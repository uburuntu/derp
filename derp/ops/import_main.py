"""One-off, best-effort import from the previous Derp's database (run once at cutover).

``python -m derp.ops.import_main --source <old database URL>`` reads the old
schema and writes into the database from the environment:

- users and chats (rows the new bot already has are left alone);
- balances become ``migration`` lots: old credits were worth about one Star, so
  they are multiplied by ``credits_per_star`` — nobody ends up with less than
  they paid for;
- a chat's old memory becomes chat notes, split by line into note-sized pieces;
- the last ``--days`` of messages, text only: the old schema kept no
  ``file_unique_id``, so media stay a kind without a file.

Every step is idempotent (keyed rows, conflict-free inserts, operation-keyed
lots), so a rerun after a failure is safe. Prints counts only.
"""

import argparse
import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from derp.config import Settings, get_settings
from derp.db.engine import Database
from derp.history.models import MESSAGE_KINDS, Chat, Message, User
from derp.memory.models import Note
from derp.money.models import CreditLot
from derp.money.wallets import credit, wallet

NOTE_CHARS = 500
BATCH = 1000
CHAT_TYPES = {"private", "group", "supergroup", "channel"}


@dataclass(slots=True)
class Counts:
    users: int = 0
    chats: int = 0
    lots: int = 0
    notes: int = 0
    messages: int = 0


def note_pieces(memory: str) -> Iterator[str]:
    """Old chat memory (free text) as note-sized pieces, split on lines."""
    piece = ""
    for raw in memory.splitlines():
        line = raw.strip()
        while len(line) > NOTE_CHARS:
            yield line[:NOTE_CHARS]
            line = line[NOTE_CHARS:]
        if piece and len(piece) + 1 + len(line) > NOTE_CHARS:
            yield piece
            piece = ""
        if line:
            piece = f"{piece}\n{line}" if piece else line
    if piece:
        yield piece


def kind_of(content_type: str | None) -> str:
    if content_type in MESSAGE_KINDS:
        return str(content_type)
    if content_type in {"new_chat_members", "left_chat_member", "pinned_message", "new_chat_title"}:
        return "service"
    return "other"


async def _rows(source: AsyncConnection, query: str, **params: Any) -> list[Any]:
    return list((await source.execute(text(query), params)).all())


async def import_main(
    source: AsyncConnection, db: Database, settings: Settings, *, bot_id: int, days: int, now: datetime
) -> Counts:
    counts = Counts()
    users = await _rows(
        source,
        "select telegram_id, is_bot, first_name, last_name, username, language_code, is_premium, credits from users",
    )
    async with db.transaction() as session:
        for u in users:
            result = await session.execute(
                insert(User)
                .values(
                    id=u.telegram_id,
                    is_bot=u.is_bot,
                    first_name=u.first_name or "",
                    last_name=u.last_name,
                    username=u.username,
                    language_code=u.language_code,
                    is_premium=bool(u.is_premium),
                )
                .on_conflict_do_nothing(index_elements=[User.id])
            )
            counts.users += result.rowcount or 0  # type: ignore[attr-defined]

    chats = await _rows(
        source, "select telegram_id, type, title, username, is_forum, llm_memory, credits from chats"
    )
    async with db.transaction() as session:
        for c in chats:
            if c.type not in CHAT_TYPES:
                continue
            result = await session.execute(
                insert(Chat)
                .values(
                    id=c.telegram_id,
                    type=c.type,
                    title=c.title if c.type != "private" else None,
                    username=c.username,
                    is_forum=bool(c.is_forum),
                )
                .on_conflict_do_nothing(index_elements=[Chat.id])
            )
            counts.chats += result.rowcount or 0  # type: ignore[attr-defined]
            for piece in note_pieces(c.llm_memory or ""):
                known = await session.scalar(
                    select(Note.id).where(Note.chat_id == c.telegram_id, Note.text == piece)
                )
                if known is None:
                    session.add(Note(chat_id=c.telegram_id, user_id=None, text=piece, author_id=None))
                    counts.notes += 1

    for kind, rows in (("user", users), ("chat", chats)):
        for row in rows:
            if (row.credits or 0) <= 0 or (kind == "chat" and row.type not in CHAT_TYPES):
                continue
            async with db.transaction() as session:  # one wallet lock per transaction
                wallet_id = (await wallet(session, kind, row.telegram_id)).id
                operation_id = f"migration:{kind}:{row.telegram_id}"
                if await session.scalar(select(CreditLot.id).where(CreditLot.operation_id == operation_id)):
                    continue
                await credit(
                    session, wallet_id, operation_id, row.credits * settings.credits_per_star, "migration"
                )
                counts.lots += 1

    forums = {c.telegram_id for c in chats if c.is_forum}
    since = now - timedelta(days=days)
    offset = 0
    while True:
        batch = await _rows(
            source,
            "select c.telegram_id as chat_id, u.telegram_id as user_id, m.telegram_message_id, m.thread_id,"
            " m.direction, m.content_type, m.text, m.media_group_id, m.reply_to_message_id, m.telegram_date,"
            " m.edited_at from messages m join chats c on c.id = m.chat_id left join users u on u.id = m.user_id"
            " where m.deleted_at is null and m.telegram_date >= :since and m.telegram_message_id > 0"
            " order by m.telegram_date, m.id limit :limit offset :offset",
            since=since,
            limit=BATCH,
            offset=offset,
        )
        if not batch:
            break
        offset += len(batch)
        async with db.transaction() as session:
            for m in batch:
                derp = m.direction == "out"
                private = m.chat_id > 0
                result = await session.execute(
                    insert(Message)
                    .values(
                        chat_id=m.chat_id,
                        message_id=m.telegram_message_id,
                        thread_id=m.thread_id if m.chat_id in forums else None,
                        user_id=bot_id if derp else m.user_id,
                        date=m.telegram_date,
                        edit_date=m.edited_at,
                        kind=kind_of(m.content_type),
                        text=m.text,
                        reply_to_message_id=m.reply_to_message_id,
                        media_group_id=m.media_group_id,
                        is_derp=derp,
                        capture="derp" if derp else ("invocation" if private else "ambient"),
                    )
                    .on_conflict_do_nothing(index_elements=[Message.chat_id, Message.message_id])
                )
                counts.messages += result.rowcount or 0  # type: ignore[attr-defined]
    return counts


async def run(source_url: str, days: int) -> Counts:
    settings = get_settings()
    settings.validate_core()
    bot_id = int(settings.telegram_bot_token.partition(":")[0])
    engine = create_async_engine(source_url)
    db = Database(settings.database_url, pool_size=2)
    try:
        async with engine.connect() as source:
            await source.execute(text("set transaction read only"))
            return await import_main(source, db, settings, bot_id=bot_id, days=days, now=datetime.now(UTC))
    finally:
        await engine.dispose()
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Import users, chats, balances, memory and recent history")
    parser.add_argument("--source", required=True, help="SQLAlchemy URL of the old database (asyncpg)")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    try:
        counts = asyncio.run(run(args.source, args.days))
    except Exception as error:  # noqa: BLE001 - report the failure class, never URLs or secrets
        raise SystemExit(f"import failed: {type(error).__name__}") from None
    print(  # noqa: T201
        f"imported: {counts.users} users, {counts.chats} chats, {counts.lots} balances,"
        f" {counts.notes} notes, {counts.messages} messages"
    )


if __name__ == "__main__":
    main()
