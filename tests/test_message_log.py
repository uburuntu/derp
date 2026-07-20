"""Telegram persistence writes all three versioned history projections."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram.types import Chat, Message, PhotoSize, User

from derp.common.message_log import upsert_message_from_message
from derp.history.snapshot import CaptureKind


@pytest.mark.asyncio
async def test_persists_normalized_projection_and_stable_attachment_reference() -> None:
    message = Message(
        message_id=7,
        date=datetime(2026, 7, 20, tzinfo=UTC),
        chat=Chat(id=-1007, type="supergroup", title="Room"),
        from_user=User(id=42, is_bot=False, first_name="Ada", username="ada"),
        caption="inspect this",
        photo=[
            PhotoSize(
                file_id="downloadable",
                file_unique_id="stable",
                width=800,
                height=600,
                file_size=123,
            )
        ],
        message_thread_id=55,
        is_topic_message=True,
    )
    session = MagicMock()
    db = MagicMock()
    db.session.return_value.__aenter__ = AsyncMock(return_value=session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "derp.common.message_log.lock_chat_history_policy",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    ambient_history_enabled=True,
                    retention_days=30,
                )
            ),
        ),
        patch(
            "derp.common.message_log.purge_expired_history",
            new_callable=AsyncMock,
        ),
        patch(
            "derp.common.message_log.upsert_message", new_callable=AsyncMock
        ) as upsert,
    ):
        await upsert_message_from_message(
            db,
            message=message,
            direction="in",
            capture=CaptureKind.AMBIENT,
        )

    values = upsert.await_args.kwargs
    assert values["thread_id"] == 55
    assert values["role"] == "user"
    assert values["capture_kind"] == "ambient"
    assert values["text"] == "inspect this"
    assert values["source_snapshot"]["schema_version"] == 1
    assert values["history_dto"]["attachments"] == [
        {
            "media_type": "photo",
            "file_id": "downloadable",
            "file_unique_id": "stable",
        }
    ]
    assert values["canonical_projection"]["attachment_types"] == ["photo"]
    assert "downloadable" not in repr(values["canonical_projection"])
    assert values["retention_expires_at"] == datetime(2026, 8, 19, tzinfo=UTC)


@pytest.mark.asyncio
async def test_outbound_messages_are_assistant_history() -> None:
    message = Message(
        message_id=8,
        date=datetime(2026, 7, 20, tzinfo=UTC),
        chat=Chat(id=-1007, type="supergroup", title="Room"),
        from_user=User(id=99, is_bot=True, first_name="Derp"),
        text="answer",
    )
    session = MagicMock()
    db = MagicMock()
    db.session.return_value.__aenter__ = AsyncMock(return_value=session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "derp.common.message_log.lock_chat_history_policy",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    ambient_history_enabled=False,
                    retention_days=30,
                )
            ),
        ),
        patch(
            "derp.common.message_log.purge_expired_history",
            new_callable=AsyncMock,
        ),
        patch(
            "derp.common.message_log.upsert_message", new_callable=AsyncMock
        ) as upsert,
    ):
        await upsert_message_from_message(db, message=message, direction="out")

    values = upsert.await_args.kwargs
    assert values["role"] == "assistant"
    assert values["history_dto"]["kind"] == "assistant_text"


@pytest.mark.asyncio
async def test_stale_ambient_classification_cannot_insert_after_disable() -> None:
    message = Message(
        message_id=9,
        date=datetime(2026, 7, 20, tzinfo=UTC),
        chat=Chat(id=-1007, type="supergroup", title="Room"),
        from_user=User(id=42, is_bot=False, first_name="Ada"),
        text="ordinary message",
    )
    db = MagicMock()
    db.session.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    db.session.return_value.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "derp.common.message_log.lock_chat_history_policy",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    ambient_history_enabled=False,
                    retention_days=30,
                )
            ),
        ),
        patch(
            "derp.common.message_log.upsert_message", new_callable=AsyncMock
        ) as upsert,
    ):
        await upsert_message_from_message(
            db,
            message=message,
            direction="in",
            capture=CaptureKind.AMBIENT,
        )

    upsert.assert_not_awaited()
