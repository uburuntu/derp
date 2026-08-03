"""PostgreSQL contracts for attaching complete tool transcripts to history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from derp.db.history import store_tool_transcript, tombstone_user_messages
from derp.db.queries import mark_message_deleted, upsert_message

pytestmark = pytest.mark.database

NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)
TOOL_ROUNDS: list[dict[str, object]] = [
    {
        "called_at": "2026-07-20T12:00:00Z",
        "returned_at": "2026-07-20T12:00:01Z",
        "calls": [
            {
                "tool_name": "search",
                "tool_call_id": "call-1",
                "arguments_json": '{"query":"release"}',
            }
        ],
        "results": [
            {
                "tool_name": "search",
                "tool_call_id": "call-1",
                "content": "Release is Tuesday.",
            }
        ],
    }
]


async def test_store_transcript_preserves_history_dto_and_replaces_retry(
    db_session, chat_factory, user_factory
) -> None:
    chat = await chat_factory(telegram_id=-9_200_001)
    user = await user_factory(telegram_id=9_200_001)
    original = {
        "schema_version": 1,
        "kind": "user_text",
        "source_message_id": 71,
        "timestamp": "2026-07-20T12:00:00Z",
        "speaker": {"id": user.telegram_id, "display_name": "Requester"},
        "text": "Find the release date",
        "attachments": [{"media_type": "photo", "file_id": "file-1"}],
        "future_field": {"preserve": True},
    }
    message = await upsert_message(
        db_session,
        chat_telegram_id=chat.telegram_id,
        user_telegram_id=user.telegram_id,
        telegram_message_id=71,
        thread_id=12,
        direction="in",
        role="user",
        content_type="text",
        text="Find the release date",
        history_dto=original,
        telegram_date=NOW,
        retention_expires_at=NOW + timedelta(days=30),
    )
    assert message is not None

    assert await store_tool_transcript(
        db_session,
        chat_telegram_id=chat.telegram_id,
        telegram_message_id=71,
        tool_rounds=TOOL_ROUNDS,
        now=NOW + timedelta(seconds=2),
    )
    await db_session.refresh(message)
    assert message.history_dto == original | {"tool_rounds": TOOL_ROUNDS}

    replacement = [
        {
            "called_at": "2026-07-20T12:01:00Z",
            "returned_at": "2026-07-20T12:01:02Z",
            "calls": [
                {
                    "tool_name": "lookup",
                    "tool_call_id": "call-2",
                    "arguments_json": "{}",
                }
            ],
            "results": [
                {
                    "tool_name": "lookup",
                    "tool_call_id": "call-2",
                    "content": "Tuesday",
                }
            ],
        }
    ]
    assert await store_tool_transcript(
        db_session,
        chat_telegram_id=chat.telegram_id,
        telegram_message_id=71,
        tool_rounds=replacement,
        now=NOW + timedelta(minutes=2),
    )
    await db_session.refresh(message)
    assert message.history_dto == original | {"tool_rounds": replacement}


async def test_store_transcript_rejects_non_request_rows(
    db_session, chat_factory, user_factory
) -> None:
    chat = await chat_factory(telegram_id=-9_200_002)
    user = await user_factory(telegram_id=9_200_002)
    assistant = await upsert_message(
        db_session,
        chat_telegram_id=chat.telegram_id,
        user_telegram_id=None,
        telegram_message_id=80,
        thread_id=None,
        direction="out",
        role="assistant",
        content_type="text",
        text="Answer",
        history_dto={"kind": "assistant_text"},
        telegram_date=NOW,
        retention_expires_at=NOW + timedelta(days=30),
    )
    outbound_user = await upsert_message(
        db_session,
        chat_telegram_id=chat.telegram_id,
        user_telegram_id=user.telegram_id,
        telegram_message_id=81,
        thread_id=None,
        direction="out",
        role="user",
        content_type="text",
        text="Invalid direction",
        history_dto={"kind": "user_text"},
        telegram_date=NOW,
        retention_expires_at=NOW + timedelta(days=30),
    )
    assert assistant is not None
    assert outbound_user is not None

    for message_id in (80, 81, 999):
        assert not await store_tool_transcript(
            db_session,
            chat_telegram_id=chat.telegram_id,
            telegram_message_id=message_id,
            tool_rounds=TOOL_ROUNDS,
            now=NOW + timedelta(seconds=2),
        )

    await db_session.refresh(assistant)
    await db_session.refresh(outbound_user)
    assert "tool_rounds" not in assistant.history_dto
    assert "tool_rounds" not in outbound_user.history_dto


async def test_store_transcript_respects_tombstone_deletion_and_retention(
    db_session, chat_factory, user_factory
) -> None:
    chat = await chat_factory(telegram_id=-9_200_003)
    user = await user_factory(telegram_id=9_200_003)
    rows = {}
    for message_id in (90, 91, 92):
        rows[message_id] = await upsert_message(
            db_session,
            chat_telegram_id=chat.telegram_id,
            user_telegram_id=user.telegram_id,
            telegram_message_id=message_id,
            thread_id=None,
            direction="in",
            role="user",
            content_type="text",
            text=f"Request {message_id}",
            history_dto={"kind": "user_text", "source_message_id": message_id},
            telegram_date=NOW,
            retention_expires_at=(
                NOW - timedelta(seconds=1)
                if message_id == 92
                else NOW + timedelta(days=30)
            ),
        )
    await tombstone_user_messages(
        db_session,
        chat_telegram_id=chat.telegram_id,
        actor_telegram_id=user.telegram_id,
        telegram_message_id=90,
        now=NOW + timedelta(seconds=1),
    )
    await mark_message_deleted(
        db_session,
        chat_telegram_id=chat.telegram_id,
        telegram_message_id=91,
        deleted_at=NOW + timedelta(seconds=1),
    )

    for message_id in rows:
        assert not await store_tool_transcript(
            db_session,
            chat_telegram_id=chat.telegram_id,
            telegram_message_id=message_id,
            tool_rounds=TOOL_ROUNDS,
            now=NOW + timedelta(seconds=2),
        )

    for message in rows.values():
        assert message is not None
        await db_session.refresh(message)
        assert "tool_rounds" not in message.history_dto


@pytest.mark.parametrize(
    "tool_rounds",
    [
        {"not": "a list"},
        [
            {
                "called_at": "2026-07-20T12:00:00Z",
                "returned_at": "2026-07-20T12:00:01Z",
                "calls": [],
                "results": [],
            }
        ],
        [
            {
                "called_at": "2026-07-20T12:00:00Z",
                "returned_at": "2026-07-20T12:00:01Z",
                "calls": [
                    {
                        "tool_name": "search",
                        "tool_call_id": "call-1",
                        "arguments_json": "not-json",
                    }
                ],
                "results": [
                    {
                        "tool_name": "other",
                        "tool_call_id": "call-1",
                        "content": "result",
                    }
                ],
            }
        ],
    ],
)
async def test_store_transcript_rejects_malformed_payloads(
    db_session, tool_rounds
) -> None:
    with pytest.raises(ValueError):
        await store_tool_transcript(
            db_session,
            chat_telegram_id=-9_200_004,
            telegram_message_id=1,
            tool_rounds=tool_rounds,
            now=NOW,
        )
