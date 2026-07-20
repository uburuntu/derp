"""Conversation history grouping and native processing contracts."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from pydantic_ai import (
    BinaryContent,
    ModelRequest,
    ModelResponse,
    RunContext,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RunUsage

from derp.history.core import (
    TokenEstimator,
    ToolCall,
    ToolResult,
    ToolRound,
    materialize_history,
)
from derp.history.media import hydration_candidate
from derp.history.persistence import project_persisted_message
from derp.history.service import (
    ConversationHistoryService,
    HistoryWindow,
    logical_turns_from_records,
    process_native_history,
    trim_history_for_media,
)
from derp.history.snapshot import (
    AttachmentMediaType,
    AttachmentSnapshot,
    CaptureKind,
    EditKind,
    EditSnapshot,
    MediaGroupKind,
    MediaGroupSnapshot,
    MessageDirection,
    ReplyKind,
    ReplySnapshot,
    SenderKind,
    SenderSnapshot,
    SnapshotRole,
    TelegramMessageSnapshot,
    TopicKind,
    TopicSnapshot,
)
from derp.history.transcript import serialize_tool_rounds

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
    tool_rounds: object | None = None,
):
    history_dto = {
        "schema_version": 1,
        "kind": f"{role}_text",
        "source_message_id": message_id,
        "timestamp": (NOW + timedelta(seconds=message_id)).isoformat(),
        "speaker": {"id": message_id, "display_name": speaker},
        "text": text,
        "attachments": attachments or [],
    }
    if tool_rounds is not None:
        history_dto["tool_rounds"] = tool_rounds
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
        history_dto=history_dto,
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


def test_restores_complete_tool_rounds_on_the_inbound_exchange() -> None:
    round_ = ToolRound(
        calls=(
            ToolCall("search", "call-1", '{"query":"weather"}'),
            ToolCall("lookup", "call-2", '{"city":"London"}'),
        ),
        results=(
            ToolResult("search", "call-1", "result one"),
            ToolResult("lookup", "call-2", "result two"),
        ),
        called_at=NOW + timedelta(seconds=2),
        returned_at=NOW + timedelta(seconds=3),
    )
    turns = logical_turns_from_records(
        [
            _record(
                1,
                "user",
                "question",
                tool_rounds=serialize_tool_rounds((round_,)),
            ),
            _record(
                4,
                "assistant",
                "answer",
                reply_to_message_id=1,
            ),
        ]
    )

    assert turns[0].tool_rounds == (round_,)
    messages = materialize_history(turns)
    assert len(messages) == 4
    call_message = messages[1]
    result_message = messages[2]
    assert isinstance(call_message, ModelResponse)
    assert isinstance(result_message, ModelRequest)
    calls = [part for part in call_message.parts if isinstance(part, ToolCallPart)]
    results = [
        part for part in result_message.parts if isinstance(part, ToolReturnPart)
    ]
    assert [(part.tool_name, part.tool_call_id) for part in calls] == [
        ("search", "call-1"),
        ("lookup", "call-2"),
    ]
    assert [(part.tool_name, part.tool_call_id) for part in results] == [
        ("search", "call-1"),
        ("lookup", "call-2"),
    ]


def test_drops_malformed_or_out_of_exchange_rounds_without_dropping_text() -> None:
    malformed_rounds = [
        {
            "called_at": "not-a-timestamp",
            "returned_at": (NOW + timedelta(seconds=3)).isoformat(),
            "calls": [
                {
                    "tool_name": "search",
                    "tool_call_id": "bad-time",
                    "arguments_json": "{}",
                }
            ],
            "results": [
                {
                    "tool_name": "search",
                    "tool_call_id": "bad-time",
                    "content": "ignored",
                }
            ],
        },
        {
            "called_at": (NOW + timedelta(seconds=2)).isoformat(),
            "returned_at": (NOW + timedelta(seconds=3)).isoformat(),
            "calls": [
                {
                    "tool_name": "search",
                    "tool_call_id": "wrong-name",
                    "arguments_json": "{}",
                }
            ],
            "results": [
                {
                    "tool_name": "other",
                    "tool_call_id": "wrong-name",
                    "content": "ignored",
                }
            ],
        },
        serialize_tool_rounds(
            (
                ToolRound(
                    calls=(ToolCall("search", "before-request", "{}"),),
                    results=(ToolResult("search", "before-request", "ignored"),),
                    called_at=NOW - timedelta(seconds=2),
                    returned_at=NOW - timedelta(seconds=1),
                ),
            )
        )[0],
    ]
    turns = logical_turns_from_records(
        [
            _record(
                1,
                "user",
                "keep the question",
                tool_rounds=malformed_rounds,
            ),
            _record(
                4,
                "assistant",
                "keep the answer",
                reply_to_message_id=1,
            ),
        ]
    )

    assert len(turns) == 1
    assert turns[0].request.text == "keep the question"
    assert turns[0].response is not None
    assert turns[0].response.text == "keep the answer"
    assert turns[0].tool_rounds == ()
    assert len(materialize_history(turns)) == 2


def test_never_attaches_tool_rounds_from_outbound_or_incomplete_rows() -> None:
    round_ = ToolRound(
        calls=(ToolCall("search", "call-1", "{}"),),
        results=(ToolResult("search", "call-1", "result"),),
        called_at=NOW + timedelta(seconds=2),
        returned_at=NOW + timedelta(seconds=3),
    )
    serialized = serialize_tool_rounds((round_,))

    incomplete = logical_turns_from_records(
        [_record(1, "user", "question", tool_rounds=serialized)]
    )
    outbound = logical_turns_from_records(
        [
            _record(1, "user", "question"),
            _record(
                4,
                "assistant",
                "answer",
                reply_to_message_id=1,
                tool_rounds=serialized,
            ),
        ]
    )

    assert incomplete[0].tool_rounds == ()
    assert outbound[0].tool_rounds == ()


def test_drops_later_round_when_a_tool_call_id_is_reused() -> None:
    first = ToolRound(
        calls=(ToolCall("search", "reused", "{}"),),
        results=(ToolResult("search", "reused", "first result"),),
        called_at=NOW + timedelta(seconds=2),
        returned_at=NOW + timedelta(seconds=2, milliseconds=500),
    )
    duplicate = ToolRound(
        calls=(ToolCall("lookup", "reused", "{}"),),
        results=(ToolResult("lookup", "reused", "second result"),),
        called_at=NOW + timedelta(seconds=3),
        returned_at=NOW + timedelta(seconds=3, milliseconds=500),
    )

    turns = logical_turns_from_records(
        [
            _record(
                1,
                "user",
                "question",
                tool_rounds=serialize_tool_rounds((first, duplicate)),
            ),
            _record(4, "assistant", "answer", reply_to_message_id=1),
        ]
    )

    assert turns[0].tool_rounds == (first,)


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


def test_media_budget_evicts_oldest_complete_turn_and_keeps_album_atomic() -> None:
    old = {
        "media_type": "photo",
        "file_id": "old",
        "file_unique_id": "stable-old",
    }
    album_first = {
        "media_type": "photo",
        "file_id": "album-1",
        "file_unique_id": "stable-album-1",
    }
    album_second = {
        "media_type": "photo",
        "file_id": "album-2",
        "file_unique_id": "stable-album-2",
    }
    turns = logical_turns_from_records(
        [
            _record(1, "user", "old", attachments=[old]),
            _record(
                2,
                "user",
                "album",
                media_group_id="album",
                attachments=[album_first],
            ),
            _record(
                3,
                "user",
                "[photo]",
                media_group_id="album",
                attachments=[album_second],
            ),
            _record(4, "user", "refer to the album"),
        ]
    )
    candidates = [
        hydration_candidate(
            AttachmentSnapshot(
                media_type=AttachmentMediaType.PHOTO,
                file_id=file_id,
                file_unique_id=f"stable-{file_id}",
                size=2,
            )
        )
        for file_id in ("old", "album-1", "album-2")
    ]
    supported = [candidate for candidate in candidates if candidate is not None]

    retained_by_items = trim_history_for_media(
        turns,
        candidates=supported,
        max_items=2,
        max_total_bytes=100,
    )
    retained_by_bytes = trim_history_for_media(
        turns,
        candidates=supported,
        max_items=10,
        max_total_bytes=4,
    )

    for retained in (retained_by_items, retained_by_bytes):
        assert [turn.request.source_message_id for turn in retained] == [2, 4]
        assert len(retained[0].request.attachments) == 2
    assert trim_history_for_media(
        turns,
        candidates=supported,
        max_items=1,
        max_total_bytes=100,
    ) == (turns[-1],)


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


def _history_record(
    message_id: int,
    text: str,
    *,
    file_id: str | None = None,
    size: int = 5,
):
    attachments = (
        (
            AttachmentSnapshot(
                media_type=AttachmentMediaType.PHOTO,
                file_id=file_id,
                file_unique_id=f"stable-{file_id}",
                size=size,
                width=100,
                height=80,
            ),
        )
        if file_id is not None
        else ()
    )
    snapshot = TelegramMessageSnapshot(
        role=SnapshotRole.USER,
        direction=MessageDirection.INBOUND,
        capture=CaptureKind.EXPLICIT,
        chat_id=-100,
        message_id=message_id,
        sent_at=NOW + timedelta(seconds=message_id),
        content_type="photo" if attachments else "text",
        topic=TopicSnapshot(TopicKind.CHAT),
        reply=ReplySnapshot(ReplyKind.NONE),
        edit=EditSnapshot(EditKind.ORIGINAL),
        media_group=MediaGroupSnapshot(MediaGroupKind.NONE),
        sender=SenderSnapshot(SenderKind.USER, 42, first_name="Ada"),
        text=None if attachments else text,
        caption=text if attachments else None,
        attachments=attachments,
    )
    projection = project_persisted_message(snapshot)
    return SimpleNamespace(
        telegram_message_id=message_id,
        telegram_date=snapshot.sent_at,
        direction="in",
        role="user",
        text=projection.text,
        content_type=snapshot.content_type,
        user=None,
        reply_to_message_id=None,
        media_group_id=None,
        history_dto=projection.history_dto,
        source_snapshot=projection.source_snapshot,
    )


class _HistoryDatabase:
    @asynccontextmanager
    async def read_session(self):
        yield MagicMock()


async def test_later_natural_reference_rehydrates_multiple_recent_images(
    monkeypatch,
) -> None:
    records = [
        _history_record(1, "an expired diagram", file_id="old"),
        _history_record(2, "the first diagram", file_id="first"),
        _history_record(3, "the second diagram", file_id="second"),
        _history_record(4, "compare those diagrams"),
    ]
    get_recent = AsyncMock(return_value=records)
    monkeypatch.setattr("derp.history.service.get_recent_messages", get_recent)
    gateway = MagicMock()

    async def download(*, reference, **_):
        return {"first": b"11111", "second": b"22222"}[reference.file_id]

    gateway.download = AsyncMock(side_effect=download)
    service = ConversationHistoryService(
        _HistoryDatabase(),  # type: ignore[arg-type]
        media_gateway=gateway,
        bot=MagicMock(),
    )

    history = await service.load_before(
        chat_id=-100,
        thread_id=None,
        current_date=NOW + timedelta(minutes=1),
        current_message_id=5,
        window=HistoryWindow(
            max_turns=10,
            max_tokens=10_000,
            query_limit=10,
            max_media=2,
            max_media_bytes=10,
        ),
    )

    assert [turn.request.source_message_id for turn in history.turns] == [2, 3, 4]
    assert history.hydrated_media == 2
    assert history.media_failures == 0
    assert gateway.download.await_count == 2
    binary_parts = [
        part
        for message in history.messages
        if isinstance(message, ModelRequest)
        for prompt in message.parts
        if isinstance(prompt, UserPromptPart) and isinstance(prompt.content, list)
        for part in prompt.content
        if isinstance(part, BinaryContent)
    ]
    assert [part.data for part in binary_parts] == [
        b"11111",
        b"22222",
    ]


async def test_declared_oversize_media_keeps_its_turn_and_missing_marker(
    monkeypatch,
) -> None:
    get_recent = AsyncMock(
        return_value=[_history_record(1, "oversize diagram", file_id="large", size=11)]
    )
    monkeypatch.setattr("derp.history.service.get_recent_messages", get_recent)
    gateway = MagicMock()
    gateway.download = AsyncMock(return_value=b"not downloaded")
    service = ConversationHistoryService(
        _HistoryDatabase(),  # type: ignore[arg-type]
        media_gateway=gateway,
        bot=MagicMock(),
    )

    history = await service.load_before(
        chat_id=-100,
        thread_id=None,
        current_date=NOW + timedelta(minutes=1),
        current_message_id=2,
        window=HistoryWindow(
            max_turns=10,
            max_tokens=10_000,
            query_limit=10,
            max_media=1,
            max_media_bytes=10,
        ),
    )

    assert [turn.request.source_message_id for turn in history.turns] == [1]
    assert history.hydrated_media == 0
    assert history.media_failures == 1
    gateway.download.assert_not_awaited()
    request = history.messages[0]
    assert isinstance(request, ModelRequest)
    prompt = request.parts[0]
    assert isinstance(prompt, UserPromptPart)
    assert '"status":"missing"' in prompt.content
