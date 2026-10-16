from typing import Any

import pytest
from aiogram.types import Message as TgMessage
from sqlalchemy import select

from derp.db.engine import Database
from derp.history.capture import upsert_chat
from derp.history.members import classify_change, track_message
from derp.history.models import ChatMember, MemberEvent


@pytest.mark.parametrize(
    ("old", "new", "kind"),
    [
        ("left", "member", "join"),
        ("member", "left", "leave"),
        ("member", "administrator", "promote"),
        ("administrator", "member", "demote"),
        ("member", "kicked", "ban"),
        ("kicked", "left", "unban"),
        ("member", "restricted", "restrict"),
        ("member", "member", None),
    ],
)
def test_classify_change(old: str, new: str, kind: str | None) -> None:
    assert classify_change(old, new) == kind


GROUP = {"id": -100, "type": "group", "title": "g"}


def tg(message_id: int, sender: dict[str, Any], **extra: Any) -> TgMessage:
    return TgMessage.model_validate(
        {"message_id": message_id, "date": 0, "chat": GROUP, "from": sender, **extra}
    )


@pytest.mark.db
async def test_track_counts_names_and_events(database: Database) -> None:
    ann = {"id": 1, "is_bot": False, "first_name": "Ann"}
    renamed = {"id": 1, "is_bot": False, "first_name": "Anna"}
    bob = {"id": 2, "is_bot": False, "first_name": "Bob"}
    async with database.transaction() as session:
        await upsert_chat(session, tg(1, ann).chat)
        await track_message(session, tg(1, ann, text="hi"), stored=True)
        await track_message(session, tg(2, renamed, text="hi"), stored=True)
        await track_message(session, tg(3, ann, new_chat_members=[bob]), stored=True)
        await track_message(session, tg(4, bob, left_chat_member=bob), stored=True)
        await track_message(session, tg(4, bob, left_chat_member=bob), stored=True)  # redelivered
    async with database.session() as session:
        member = await session.get(ChatMember, (-100, 1))
        assert member is not None and (member.message_count, member.names) == (3, ["Anna", "Ann"])
        bob_row = await session.get(ChatMember, (-100, 2))
        assert bob_row is not None and bob_row.status == "left"
        kinds = list(await session.scalars(select(MemberEvent.kind).order_by(MemberEvent.id)))
        assert kinds == ["join", "leave"]
