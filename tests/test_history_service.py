"""Conversation history grouping and native processing contracts."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from pydantic_ai import (
    ModelRequest,
    ModelResponse,
    RunContext,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.usage import RunUsage

from derp.history.core import TokenEstimator
from derp.history.service import (
    HistoryWindow,
    logical_turns_from_records,
    process_native_history,
)

NOW = datetime(2026, 7, 20, tzinfo=UTC)


def _record(
    message_id: int,
    role: str,
    text: str,
    *,
    speaker: str = "Ada",
    reply_to_message_id: int | None = None,
    media_group_id: str | None = None,
    attachments: list[dict[str, str]] | None = None,
):
    return SimpleNamespace(
        telegram_message_id=message_id,
        telegram_date=NOW + timedelta(seconds=message_id),
        direction="out" if role == "assistant" else "in",
        role=role,
        text=text,
        content_type="text",
        user=None,
        reply_to_message_id=reply_to_message_id,
        media_group_id=media_group_id,
        history_dto={
            "schema_version": 1,
            "kind": f"{role}_text",
            "source_message_id": message_id,
            "timestamp": (NOW + timedelta(seconds=message_id)).isoformat(),
            "speaker": {"id": message_id, "display_name": speaker},
            "text": text,
            "attachments": attachments or [],
        },
    )


def test_groups_ambient_messages_and_combines_split_assistant_delivery() -> None:
    turns = logical_turns_from_records(
        [
            _record(1, "user", "ambient one", speaker="Ada"),
            _record(2, "user", "question", speaker="Grace"),
            _record(
                3,
                "assistant",
                "answer part one",
                speaker="Derp",
                reply_to_message_id=2,
            ),
            _record(
                4,
                "assistant",
                "answer part two",
                speaker="Derp",
                reply_to_message_id=3,
            ),
        ]
    )

    assert len(turns) == 2
    assert turns[0].response is None
    assert turns[1].request.speaker.display_name == "Grace"
    assert turns[1].response is not None
    assert turns[1].response.text == "answer part one\nanswer part two"
    assert turns[1].response.source_message_id == 4


def test_pairs_assistant_chain_to_replied_turn_not_latest_user() -> None:
    turns = logical_turns_from_records(
        [
            _record(1, "user", "invocation", speaker="Ada"),
            _record(2, "user", "concurrent ambient", speaker="Grace"),
            _record(
                3,
                "assistant",
                "answer part one",
                speaker="Derp",
                reply_to_message_id=1,
            ),
            _record(
                4,
                "assistant",
                "answer part two",
                speaker="Derp",
                reply_to_message_id=3,
            ),
        ]
    )

    assert [turn.request.text for turn in turns] == [
        "invocation",
        "concurrent ambient",
    ]
    assert turns[0].response is not None
    assert turns[0].response.text == "answer part one\nanswer part two"
    assert turns[1].response is None


def test_keeps_assistant_fragments_separate_across_reply_roots() -> None:
    turns = logical_turns_from_records(
        [
            _record(1, "user", "first", speaker="Ada"),
            _record(2, "user", "second", speaker="Grace"),
            _record(
                3,
                "assistant",
                "first answer",
                speaker="Derp",
                reply_to_message_id=1,
            ),
            _record(
                4,
                "assistant",
                "second answer",
                speaker="Derp",
                reply_to_message_id=2,
            ),
        ]
    )

    assert [turn.response and turn.response.text for turn in turns] == [
        "first answer",
        "second answer",
    ]


def test_groups_album_rows_into_one_atomic_user_turn() -> None:
    first_attachment = {
        "media_type": "image",
        "file_id": "file-1",
        "file_unique_id": "unique-1",
    }
    second_attachment = {
        "media_type": "image",
        "file_id": "file-2",
        "file_unique_id": "unique-2",
    }
    turns = logical_turns_from_records(
        [
            _record(
                1,
                "user",
                "album caption",
                media_group_id="album-1",
                attachments=[first_attachment],
            ),
            _record(
                2,
                "user",
                "[photo]",
                media_group_id="album-1",
                attachments=[second_attachment],
            ),
            _record(3, "user", "later message", speaker="Grace"),
            _record(
                4,
                "assistant",
                "album answer",
                speaker="Derp",
                reply_to_message_id=2,
            ),
        ]
    )

    assert len(turns) == 2
    assert turns[0].request.source_message_id == 1
    assert turns[0].request.text == "album caption\n[photo]"
    assert [item.file_unique_id for item in turns[0].request.attachments] == [
        "unique-1",
        "unique-2",
    ]
    assert turns[0].response is not None
    assert turns[0].response.text == "album answer"
    assert turns[1].request.text == "later message"


def test_ignores_orphan_assistant_rows_without_reordering_user_turns() -> None:
    turns = logical_turns_from_records(
        [
            _record(
                1,
                "assistant",
                "orphan before query window",
                speaker="Derp",
                reply_to_message_id=999,
            ),
            _record(2, "user", "first", speaker="Ada"),
            _record(
                3,
                "assistant",
                "orphan without reply",
                speaker="Derp",
            ),
            _record(4, "user", "second", speaker="Grace"),
        ]
    )

    assert [turn.request.text for turn in turns] == ["first", "second"]
    assert all(turn.response is None for turn in turns)


def test_native_processor_keeps_current_oversized_turn() -> None:
    from derp.history.core import (
        AssistantTextTurn,
        LogicalTurn,
        Speaker,
        UserTextTurn,
        materialize_history,
    )

    speaker = Speaker(1, "Ada")
    bot = Speaker(2, "Derp")
    turns = tuple(
        LogicalTurn(
            request=UserTextTurn(i, NOW + timedelta(seconds=i), speaker, "q" * 50),
            response=AssistantTextTurn(
                i + 10,
                NOW + timedelta(seconds=i + 10),
                bot,
                "a" * 50,
            ),
        )
        for i in (1, 2, 3)
    )
    messages = [
        *materialize_history(turns),
        ModelRequest(parts=[UserPromptPart("current" * 50)]),
    ]
    deps = SimpleNamespace(
        history_window=HistoryWindow(max_turns=2, max_tokens=1, query_limit=2)
    )
    ctx = RunContext(
        deps=deps,
        model=MagicMock(),
        usage=RunUsage(),
    )

    processed = process_native_history(ctx, messages)

    assert len(processed) == 1
    assert isinstance(processed[0], ModelRequest)
    assert TokenEstimator().estimate_text(repr(processed[0])) > 1


def test_native_processor_counts_window_as_prior_turns() -> None:
    messages = [
        ModelRequest(parts=[UserPromptPart("expired")]),
        ModelResponse(parts=[TextPart("expired answer")]),
        ModelRequest(parts=[UserPromptPart("oldest")]),
        ModelResponse(parts=[TextPart("oldest answer")]),
        ModelRequest(parts=[UserPromptPart("recent")]),
        ModelResponse(parts=[TextPart("recent answer")]),
        ModelRequest(parts=[UserPromptPart("current")]),
    ]
    deps = SimpleNamespace(
        history_window=HistoryWindow(
            max_turns=2,
            max_tokens=10_000,
            query_limit=2,
        )
    )
    ctx = RunContext(
        deps=deps,
        model=MagicMock(),
        usage=RunUsage(),
    )

    processed = process_native_history(ctx, messages)

    assert processed == messages[2:]
