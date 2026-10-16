from typing import Any

import pytest
from aiogram.types import Message as TgMessage
from sqlalchemy import func, select

from derp.db.engine import Database
from derp.history.capture import (
    classify,
    media_rows,
    payload_for,
    record_outgoing,
    store_message,
    upsert_chat,
)
from derp.history.models import Chat, ForumTopic, Message, MessageMedia, User

GROUP = {"id": -100, "type": "supergroup", "title": "Chat", "is_forum": True}
ANN = {"id": 1, "is_bot": False, "first_name": "Ann", "language_code": "ru"}


def tg(**fields: Any) -> TgMessage:
    data: dict[str, Any] = {"message_id": 10, "date": 1_700_000_000, "chat": GROUP, "from": ANN} | fields
    return TgMessage.model_validate(data)


def test_classify_and_payloads() -> None:
    dice = tg(dice={"emoji": "🎲", "value": 6})
    assert classify(dice) == "dice"
    assert payload_for(dice, "dice") == {"emoji": "🎲", "value": 6}
    contact = tg(contact={"phone_number": "+100", "first_name": "Bob", "user_id": 2})
    assert payload_for(contact, "contact")["phone_number"] == "+100"
    joined = tg(new_chat_members=[{"id": 2, "is_bot": False, "first_name": "Bob"}])
    assert classify(joined) == "service"
    assert payload_for(joined, "service") == {"event": "new_chat_members", "data": [{"id": 2, "name": "Bob"}]}
    poll = tg(
        poll={
            "id": "p1", "question": "Q?", "options": [{"text": "a", "voter_count": 0, "persistent_id": "o1"}, {"text": "b", "voter_count": 0, "persistent_id": "o2"}],
            "total_voter_count": 0, "is_closed": False, "is_anonymous": False, "type": "regular",
            "allows_multiple_answers": False, "allows_revoting": True, "members_only": False,
        }
    )  # fmt: skip
    assert payload_for(poll, "poll")["options"] == ["a", "b"]


def test_media_keeps_largest_photo_and_sticker_fields() -> None:
    photo = tg(
        photo=[
            {"file_id": "s", "file_unique_id": "us", "width": 90, "height": 90},
            {"file_id": "l", "file_unique_id": "ul", "width": 1280, "height": 960},
        ],
        caption="look",
    )
    (row,) = media_rows(photo)
    assert row["file_id"] == "l" and row["idx"] == 0
    sticker = tg(
        sticker={"file_id": "st", "file_unique_id": "ust", "type": "regular", "width": 512, "height": 512,
                 "is_animated": False, "is_video": False, "emoji": "😂", "set_name": "memes"}
    )  # fmt: skip
    (row,) = media_rows(sticker)
    assert (row["sticker_emoji"], row["sticker_set"]) == ("😂", "memes")


pytestmark = pytest.mark.db


async def test_store_edit_and_outgoing(database: Database) -> None:
    async with database.transaction() as session:
        await upsert_chat(session, tg().chat)
        await store_message(
            session, tg(text="hello derp", message_thread_id=7, is_topic_message=True), capture="invocation"
        )
        await store_message(session, tg(message_id=11, text="ambient"), capture="ambient")
    async with database.transaction() as session:
        edited = tg(text="hello again", edit_date=1_700_000_100, message_thread_id=7, is_topic_message=True)
        await store_message(session, edited, capture="ambient")
        derp = tg(message_id=12, text="hi!", **{"from": {"id": 99, "is_bot": True, "first_name": "Derp"}})
        await record_outgoing(session, [derp])
    async with database.session() as session:
        first = await session.get(Message, (-100, 10))
        assert first is not None
        assert (first.text, first.capture, first.thread_id) == ("hello again", "invocation", 7)
        assert first.edit_date is not None
        outgoing = await session.get(Message, (-100, 12))
        assert outgoing is not None and outgoing.is_derp and outgoing.capture == "derp"
        user = await session.get(User, 1)
        assert user is not None and user.language_code == "ru"
        chat = await session.get(Chat, -100)
        assert chat is not None and chat.is_forum
        hits = await session.scalars(
            select(Message.message_id).where(
                Message.tsv.op("@@")(func.websearch_to_tsquery("simple", "again"))
            )
        )
        assert list(hits) == [10]


async def test_unstorable_and_topics(database: Database) -> None:
    async with database.transaction() as session:
        await upsert_chat(session, tg().chat)
        assert not await store_message(session, tg(message_id=0, text="ephemeral"), capture="derp")
        created = tg(
            message_id=20, message_thread_id=20, forum_topic_created={"name": "Memes", "icon_color": 1}
        )
        await store_message(session, created, capture="ambient")
        await store_message(
            session, tg(message_id=21, message_thread_id=20, forum_topic_closed={}), capture="ambient"
        )
        await store_message(
            session,
            tg(message_id=22, photo=[{"file_id": "p", "file_unique_id": "up", "width": 1, "height": 1}]),
            capture="ambient",
        )
    async with database.session() as session:
        topic = await session.get(ForumTopic, (-100, 20))
        assert topic is not None and topic.name == "Memes" and topic.closed
        media = await session.get(MessageMedia, (-100, 22, 0))
        assert media is not None and media.type == "photo"


@pytest.mark.db
async def test_an_edit_that_swaps_the_photo_drops_notes_about_the_old_one(database: Database) -> None:
    def shot(unique: str, **extra: Any) -> TgMessage:
        size = {"file_id": f"f-{unique}", "file_unique_id": unique, "width": 10, "height": 10}
        return tg(photo=[size], caption="look", **extra)

    async with database.transaction() as session:
        await upsert_chat(session, shot("a").chat)
        await store_message(session, shot("a"), capture="ambient")
        media = await session.get(MessageMedia, (-100, 10, 0))
        message = await session.get(Message, (-100, 10))
        assert media is not None and message is not None
        media.description, message.extra_text = "a cat", "a cat"
    async with database.transaction() as session:
        await store_message(session, shot("a", edit_date=1_700_000_100), capture="ambient")  # caption edit
    async with database.session() as session:
        assert (await session.get(MessageMedia, (-100, 10, 0))).description == "a cat"  # type: ignore[union-attr]
    async with database.transaction() as session:
        await store_message(session, shot("b", edit_date=1_700_000_200), capture="ambient")  # new photo
    async with database.session() as session:
        media = await session.get(MessageMedia, (-100, 10, 0))
        message = await session.get(Message, (-100, 10))
        assert media is not None and (media.file_unique_id, media.description) == ("b", None)
        assert message is not None and message.extra_text is None
