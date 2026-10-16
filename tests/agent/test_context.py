from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from derp.agent import context
from derp.agent.context import HISTORY_CLOSE, Window, build_window, clip_tool_results
from derp.agent.runs import finish_run, record_output, start_run
from derp.db.engine import Database
from derp.history.models import Chat, Message, MessageMedia, Reaction, User

CHAT = -500
UTC_TZ = ZoneInfo("UTC")
START = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)


def message(message_id: int, user_id: int | None, text: str, **extra: object) -> Message:
    return Message(
        chat_id=CHAT,
        message_id=message_id,
        user_id=user_id,
        date=START + timedelta(minutes=message_id),
        kind=str(extra.pop("kind", "text")),
        text=text,
        capture=str(extra.pop("capture", "ambient")),
        is_derp=bool(extra.pop("is_derp", False)),
        payload={},
        **extra,
    )


def run_history() -> list[ModelRequest | ModelResponse]:
    return [
        ModelRequest(parts=[UserPromptPart("derp, find cats")], instructions="persona"),
        ModelResponse(parts=[ToolCallPart("search_history", {"query": "cats"}, tool_call_id="t1")]),
        ModelRequest(parts=[ToolReturnPart("search_history", "x" * 5000, tool_call_id="t1")]),
        ModelResponse(parts=[TextPart("found 3 cats")]),
    ]


def test_replay_drops_prompt_and_clips_results() -> None:
    replay = clip_tool_results(list(run_history()))
    assert len(replay) == 3
    assert isinstance(replay[0], ModelResponse)
    result = replay[1].parts[0]
    assert isinstance(result, ToolReturnPart)
    assert len(result.model_response_str()) < 1600


async def seed(database: Database) -> None:
    async with database.transaction() as session:
        session.add(Chat(id=CHAT, type="supergroup", title="Cats"))
        session.add_all([User(id=1, first_name="Ann"), User(id=2, first_name="Bob")])
        await session.flush()
        session.add_all(
            [
                message(1, 1, "anyone seen cats?"),
                message(2, 2, "look", kind="photo"),
                message(3, 1, "derp, find cats", capture="invocation"),
                message(4, None, "found 3 cats", capture="derp", is_derp=True),
                message(5, 2, "nice", reply_to_message_id=4),
                message(6, 2, "other topic", thread_id=77),
                message(7, 1, "current message"),
            ]
        )
        await session.flush()
        session.add(
            MessageMedia(
                chat_id=CHAT,
                message_id=2,
                idx=0,
                type="photo",
                file_id="f",
                bot_id=1,
                file_unique_id="u",
                description="a grey cat",
            )
        )
        session.add(Reaction(chat_id=CHAT, message_id=4, actor_id=2, reaction="🔥"))
        run = await start_run(
            session,
            chat_id=CHAT,
            user_id=1,
            trigger="message",
            role="chat",
            funding="paid",
            trigger_message_id=3,
        )
        finish_run(session, run, run_history(), status="completed")
        record_output(session, run, 4, "message")


@pytest.mark.db
async def test_window_replays_runs_between_member_transcripts(database: Database) -> None:
    await seed(database)
    async with database.session() as session:
        window = await build_window(
            session, chat_id=CHAT, thread_id=None, before_message_id=7, budget=10_000, tz=UTC_TZ
        )
    kinds = [type(m).__name__ for m in window.messages]
    assert kinds == ["ModelRequest", "ModelResponse", "ModelRequest", "ModelResponse", "ModelRequest"]
    first = window.messages[0].parts[0]
    assert isinstance(first, UserPromptPart) and isinstance(first.content, str)
    assert "<chat_history" in first.content
    assert "#2 Bob: look [photo: a grey cat]" in first.content
    assert "#3 Ann: derp, find cats" in first.content
    last = window.messages[-1].parts[0]
    assert isinstance(last, UserPromptPart) and "#5 Bob ↩#4: nice" in str(last.content)
    assert "other topic" not in str(window.messages) and "current message" not in str(window.messages)
    assert (window.count, window.oldest_message_id) == (5, 1)


@pytest.mark.db
async def test_small_budget_keeps_newest_and_never_opens_mid_run(database: Database) -> None:
    await seed(database)
    async with database.session() as session:
        window = await build_window(
            session, chat_id=CHAT, thread_id=None, before_message_id=7, budget=30, tz=UTC_TZ
        )
        assert [type(m).__name__ for m in window.messages] == ["ModelRequest"]
        assert window.oldest_message_id == 5
        topic = await build_window(
            session, chat_id=CHAT, thread_id=77, before_message_id=7, budget=1000, tz=UTC_TZ
        )
        assert "other topic" in str(topic.messages) and topic.count == 1


@pytest.mark.db
async def test_kept_window_start_extends_the_prefix_until_it_outgrows_the_budget(database: Database) -> None:
    async with database.transaction() as session:
        session.add(Chat(id=CHAT, type="supergroup", title="Busy"))
        session.add(User(id=1, first_name="Ann"))
        await session.flush()
        session.add_all([message(i, 1, f"message number {i} about nothing much") for i in range(1, 121)])

    def transcript(window: Window) -> str:
        first = window.messages[0].parts[0]
        assert isinstance(first, UserPromptPart) and isinstance(first.content, str)
        return first.content

    async with database.session() as session:
        fresh = await build_window(
            session, chat_id=CHAT, thread_id=None, before_message_id=41, budget=150, tz=UTC_TZ
        )
        start = fresh.oldest_message_id
        assert start is not None and start > 1
        kept = await build_window(
            session, chat_id=CHAT, thread_id=None, before_message_id=44, budget=150, tz=UTC_TZ, start_at=start
        )
        assert kept.oldest_message_id == start and kept.tokens > fresh.tokens
        # Same start, new messages appended: the old prompt text is a prefix of the new one (cache hit).
        assert transcript(kept).startswith(transcript(fresh).removesuffix(f"\n{HISTORY_CLOSE}"))
        later = await build_window(
            session,
            chat_id=CHAT,
            thread_id=None,
            before_message_id=120,
            budget=150,
            tz=UTC_TZ,
            start_at=start,
        )
        assert later.oldest_message_id is not None and later.oldest_message_id > start and later.tokens <= 150


@pytest.mark.db
async def test_row_capped_windows_are_kept_too(database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(context, "SCAN_LIMIT", 10)  # busy chat of short messages: rows run out before tokens
    async with database.transaction() as session:
        session.add(Chat(id=CHAT, type="supergroup", title="Busy"))
        session.add(User(id=1, first_name="Ann"))
        await session.flush()
        session.add_all([message(i, 1, f"hi {i}") for i in range(1, 41)])
    async with database.session() as session:
        fresh = await build_window(
            session, chat_id=CHAT, thread_id=None, before_message_id=21, budget=10_000, tz=UTC_TZ
        )
        assert (fresh.count, fresh.oldest_message_id) == (10, 11)
        kept = await build_window(
            session, chat_id=CHAT, thread_id=None, before_message_id=24, budget=10_000, tz=UTC_TZ, start_at=11
        )
        assert (kept.count, kept.oldest_message_id) == (13, 11)
        reset = await build_window(
            session, chat_id=CHAT, thread_id=None, before_message_id=30, budget=10_000, tz=UTC_TZ, start_at=11
        )
        assert (reset.count, reset.oldest_message_id) == (10, 20)
