from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from aiogram.types import Message as TgMessage
from sqlalchemy import func, select

from derp.db.engine import Database
from derp.history.capture import (
    classify,
    media_rows,
    message_values,
    payload_for,
    record_outgoing,
    refresh_notes,
    store_message,
    unstored_view,
    upsert_chat,
)
from derp.history.models import Chat, ForumTopic, Message, MessageMedia, User
from derp.history.render import render_line
from derp.history.retention import forget_message

BOT_ID = 777000111

GROUP = {"id": -100, "type": "supergroup", "title": "Chat", "is_forum": True}
ANN = {"id": 1, "is_bot": False, "first_name": "Ann", "language_code": "ru"}


def tg(**fields: Any) -> TgMessage:
    data: dict[str, Any] = {"message_id": 10, "date": 1_700_000_000, "chat": GROUP, "from": ANN} | fields
    return TgMessage.model_validate(data)


def test_a_message_never_stored_renders_like_a_stored_one() -> None:
    bob = {"id": 2, "is_bot": False, "first_name": "Bob", "last_name": "Lee", "username": "bob"}
    ephemeral = tg(
        message_id=0,
        **{"from": bob},
        caption="what is this",
        photo=[{"file_id": "p", "file_unique_id": "u-p", "width": 9, "height": 9}],
        forward_origin={"type": "hidden_user", "sender_user_name": "Ghost", "date": 1_700_000_000},
        quote={"text": "x" * 300, "position": 0},
    )
    line = render_line(unstored_view(ephemeral, BOT_ID), ZoneInfo("UTC"))
    # Same header as any line (no #id: there is nothing to cite), the quote clipped, the media shown.
    assert line == f"[22:13] Bob Lee @bob (fwd from Ghost) «{'x' * 199}…»: what is this [photo]"


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
    (row,) = media_rows(photo, BOT_ID)
    assert row["file_id"] == "l" and row["idx"] == 0 and row["bot_id"] == BOT_ID
    sticker = tg(
        sticker={"file_id": "st", "file_unique_id": "ust", "type": "regular", "width": 512, "height": 512,
                 "is_animated": False, "is_video": False, "emoji": "😂", "set_name": "memes"}
    )  # fmt: skip
    (row,) = media_rows(sticker, BOT_ID)
    assert (row["sticker_emoji"], row["sticker_set"]) == ("😂", "memes")
    assert row["mime_type"] == "image/webp" and row["thumb_file_id"] is None


def thumb(name: str) -> dict[str, Any]:
    return {"file_id": f"t-{name}", "file_unique_id": f"ut-{name}", "width": 320, "height": 180}


STICKER = {"file_unique_id": "us", "type": "regular", "width": 512, "height": 512, "emoji": "🐸"}
CLIP = {"width": 640, "height": 360, "duration": 3}


@pytest.mark.parametrize(
    ("kind", "item", "mime"),
    [
        ("sticker", STICKER | {"file_id": "tgs", "is_animated": True, "is_video": False}, "application/x-tgsticker"),
        ("sticker", STICKER | {"file_id": "webm", "is_animated": False, "is_video": True}, "video/webm"),
        ("animation", CLIP | {"file_id": "gif", "file_unique_id": "ug", "mime_type": "video/mp4"}, "video/mp4"),
        ("video", CLIP | {"file_id": "vid", "file_unique_id": "uv", "mime_type": "video/mp4"}, "video/mp4"),
        ("video_note", {"file_id": "vn", "file_unique_id": "un", "length": 240, "duration": 5}, None),
        ("document", {"file_id": "pdf", "file_unique_id": "ud", "mime_type": "application/pdf"}, "application/pdf"),
    ],
)  # fmt: skip
def test_media_keeps_the_preview_picture_of_what_isnt_one(
    kind: str, item: dict[str, Any], mime: str | None
) -> None:
    fields = {kind: item | {"thumbnail": thumb(kind)}}
    if kind == "animation":  # Telegram sends a GIF's file as a document too: one row, the animation
        fields["document"] = {"file_id": item["file_id"], "file_unique_id": item["file_unique_id"],
                              "mime_type": item["mime_type"], "file_name": "funny.gif.mp4"}  # fmt: skip
    (row,) = media_rows(tg(**fields), BOT_ID)
    assert row["type"] == kind and row["mime_type"] == mime and row["bot_id"] == BOT_ID
    assert (row["thumb_file_id"], row["thumb_unique_id"]) == (f"t-{kind}", f"ut-{kind}")


def size(name: str, side: int) -> dict[str, Any]:
    return {"file_id": name, "file_unique_id": f"u-{name}", "width": side, "height": side}


def paragraph(text: Any) -> dict[str, Any]:
    return {"type": "paragraph", "text": text}


def test_a_rich_message_is_text_and_its_media_blocks_are_rows() -> None:
    caption = {"text": "the view", "credit": "me"}
    rich = tg(
        rich_message={
            "blocks": [
                {"type": "heading", "text": "Trip", "size": 1},
                paragraph(["Day one, ", {"type": "url", "text": "the map", "url": "https://example.com/secret"}]),
                {"type": "collage", "blocks": [
                    {"type": "photo", "photo": [size("small", 90), size("big", 1280)], "caption": caption},
                    {"type": "video", "video": CLIP | {"file_id": "clip", "file_unique_id": "uc",
                                                        "thumbnail": thumb("clip")}},
                ]},
                {"type": "list", "items": [{"label": "•", "blocks": [paragraph("pack socks")]}]},
                {"type": "table", "cells": [[{"align": "left", "valign": "top", "text": "a"},
                                             {"align": "left", "valign": "top", "text": "b"}]]},
                {"type": "paragraph", "text": [{"type": "custom_emoji", "custom_emoji_id": "42",
                                                "alternative_text": "🔥"}]},
            ]
        }
    )  # fmt: skip
    values = message_values(rich, capture="ambient")
    assert values["kind"] == "text" and values["raw"] is None
    assert values["text"] == "Trip\nDay one, the map\nthe view\npack socks\na | b\n🔥"  # never the URL
    photo, video = media_rows(rich, BOT_ID)
    assert (photo["type"], photo["file_id"], photo["idx"]) == ("photo", "big", 0)
    assert (video["type"], video["file_id"], video["thumb_file_id"], video["idx"]) == (
        "video",
        "clip",
        "t-clip",
        1,
    )


def test_media_of_a_message_quoted_from_another_chat_are_rows() -> None:
    quoting = tg(
        text="lol look",
        external_reply={
            "origin": {"type": "channel", "date": 1_700_000_000, "message_id": 5,
                       "chat": {"id": -200, "type": "channel", "title": "News"}},
            "photo": [size("q-small", 90), size("q-big", 800)],
        },
    )  # fmt: skip
    (row,) = media_rows(quoting, BOT_ID)
    assert (row["type"], row["file_id"], row["bot_id"]) == ("photo", "q-big", BOT_ID)
    assert message_values(quoting, capture="ambient")["text"] == "lol look"


pytestmark = pytest.mark.db


async def test_store_edit_and_outgoing(database: Database) -> None:
    async with database.transaction() as session:
        await upsert_chat(session, tg().chat)
        await store_message(
            session,
            tg(text="hello derp", message_thread_id=7, is_topic_message=True),
            capture="invocation",
            bot_id=BOT_ID,
        )
        await store_message(session, tg(message_id=11, text="ambient"), capture="ambient", bot_id=BOT_ID)
    async with database.transaction() as session:
        edited = tg(text="hello again", edit_date=1_700_000_100, message_thread_id=7, is_topic_message=True)
        await store_message(session, edited, capture="ambient", bot_id=BOT_ID)
        derp = tg(message_id=12, text="hi!", **{"from": {"id": 99, "is_bot": True, "first_name": "Derp"}})
        await record_outgoing(session, [derp], BOT_ID)
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
        assert not await store_message(
            session, tg(message_id=0, text="ephemeral"), capture="derp", bot_id=BOT_ID
        )
        created = tg(
            message_id=20, message_thread_id=20, forum_topic_created={"name": "Memes", "icon_color": 1}
        )
        await store_message(session, created, capture="ambient", bot_id=BOT_ID)
        await store_message(
            session,
            tg(message_id=21, message_thread_id=20, forum_topic_closed={}),
            capture="ambient",
            bot_id=BOT_ID,
        )
        await store_message(
            session,
            tg(message_id=22, photo=[{"file_id": "p", "file_unique_id": "up", "width": 1, "height": 1}]),
            capture="ambient",
            bot_id=BOT_ID,
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
        await store_message(session, shot("a"), capture="ambient", bot_id=BOT_ID)
        media = await session.get(MessageMedia, (-100, 10, 0))
        message = await session.get(Message, (-100, 10))
        assert media is not None and message is not None
        media.description, message.extra_text = "a cat", "a cat"
    async with database.transaction() as session:
        await store_message(
            session, shot("a", edit_date=1_700_000_100), capture="ambient", bot_id=BOT_ID
        )  # caption edit
    async with database.session() as session:
        assert (await session.get(MessageMedia, (-100, 10, 0))).description == "a cat"  # type: ignore[union-attr]
    async with database.transaction() as session:
        await store_message(
            session, shot("b", edit_date=1_700_000_200), capture="ambient", bot_id=BOT_ID
        )  # new photo
    async with database.session() as session:
        media = await session.get(MessageMedia, (-100, 10, 0))
        message = await session.get(Message, (-100, 10))
        assert media is not None and (media.file_unique_id, media.description) == ("b", None)
        assert message is not None and message.extra_text is None


@pytest.mark.db
async def test_search_text_reads_every_file_note_and_keeps_them_across_a_swap(database: Database) -> None:
    def post(photo: str, **extra: Any) -> TgMessage:
        size = {"file_id": f"f-{photo}", "file_unique_id": photo, "width": 10, "height": 10}
        quoted = {"origin": {"type": "hidden_user", "sender_user_name": "Ghost", "date": 1}, "photo": [size]}
        video = CLIP | {"file_id": "clip", "file_unique_id": "uclip"}
        return tg(video=video, external_reply=quoted, **extra)

    async with database.transaction() as session:
        await upsert_chat(session, post("a").chat)
        await store_message(session, post("a"), capture="ambient", bot_id=BOT_ID)
        clip, photo = [await session.get(MessageMedia, (-100, 10, idx)) for idx in (0, 1)]
        assert clip is not None and photo is not None
        clip.description, clip.transcript, photo.description = "a kitchen", "buy bread", "a cat"
        await refresh_notes(session, -100, 10)
    async with database.session() as session:
        message = await session.get(Message, (-100, 10))
        assert message is not None and message.extra_text == "a kitchen — buy bread a cat"
    async with database.transaction() as session:  # the quoted picture changes; the clip's notes stay
        await store_message(session, post("b", edit_date=1_700_000_100), capture="ambient", bot_id=BOT_ID)
    async with database.session() as session:
        message = await session.get(Message, (-100, 10))
        assert message is not None and message.extra_text == "a kitchen — buy bread"


@pytest.mark.db
async def test_an_edit_never_restores_a_forgotten_message(database: Database) -> None:
    def shot(unique: str, caption: str, **extra: Any) -> TgMessage:
        size = {"file_id": f"f-{unique}", "file_unique_id": unique, "width": 10, "height": 10}
        return tg(photo=[size], caption=caption, **extra)

    async with database.transaction() as session:
        await upsert_chat(session, shot("a", "secret").chat)
        await store_message(session, shot("a", "secret"), capture="ambient", bot_id=BOT_ID)
    async with database.transaction() as session:
        await forget_message(session, -100, 10, datetime.now(UTC))
    async with database.transaction() as session:
        edited = shot("b", "still secret", edit_date=1_700_000_100)
        assert not await store_message(session, edited, capture="ambient", bot_id=BOT_ID)
    async with database.session() as session:
        message = await session.get(Message, (-100, 10))
        assert message is not None and message.deleted_at is not None
        assert (message.text, message.entities, message.payload, message.raw) == (None, None, {}, None)
        assert await session.scalar(select(func.count()).select_from(MessageMedia)) == 0
        searchable = select(func.count()).where(
            Message.tsv.bool_op("@@")(func.plainto_tsquery("simple", "secret"))
        )
        assert await session.scalar(searchable) == 0
