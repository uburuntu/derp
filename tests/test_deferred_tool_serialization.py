"""Deferred history is round-tripped through Pydantic AI's native adapter."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic_ai import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolApproved,
    ToolCallPart,
    UserPromptPart,
)

from derp.approvals import (
    HISTORY_SCHEMA_VERSION,
    DeferredToolSerializationError,
    DeferredToolSnapshot,
    DeferredToolStatus,
    ResumeLease,
    deserialize_history,
    serialize_deferred_request,
)
from derp.operations import OperationId, QuoteId


def _history(call: ToolCallPart) -> list[ModelMessage]:
    return [
        ModelRequest(parts=[UserPromptPart("private prompt")]),
        ModelResponse(parts=[call]),
    ]


def test_validated_call_and_original_history_round_trip() -> None:
    call = ToolCallPart(
        "generate_image",
        '{"prompt":"private prompt","count":1}',
        "tool-call-1",
    )

    arguments, serialized = serialize_deferred_request(call, _history(call))
    restored = deserialize_history(
        serialized,
        schema_version=HISTORY_SCHEMA_VERSION,
    )

    assert arguments == {"prompt": "private prompt", "count": 1}
    assert isinstance(restored[-1], ModelResponse)
    restored_call = restored[-1].parts[0]
    assert isinstance(restored_call, ToolCallPart)
    assert restored_call.tool_call_id == call.tool_call_id
    assert restored_call.args_as_dict(raise_if_invalid=True) == arguments


@pytest.mark.parametrize(
    "call",
    [
        ToolCallPart("generate_image", "not-json", "tool-call-1"),
        ToolCallPart("generate_image", "[]", "tool-call-1"),
    ],
)
def test_invalid_tool_arguments_are_rejected_without_echoing_content(
    call: ToolCallPart,
) -> None:
    with pytest.raises(DeferredToolSerializationError) as raised:
        serialize_deferred_request(call, _history(call))

    assert str(call.args) not in str(raised.value)


def test_call_must_match_the_final_trusted_response() -> None:
    persisted = ToolCallPart("generate_image", {"prompt": "original"}, "call-1")
    substituted = ToolCallPart("generate_image", {"prompt": "changed"}, "call-1")

    with pytest.raises(DeferredToolSerializationError, match="does not match"):
        serialize_deferred_request(substituted, _history(persisted))


def test_unknown_history_schema_fails_closed() -> None:
    call = ToolCallPart("generate_image", {}, "call-1")
    _, serialized = serialize_deferred_request(call, _history(call))

    with pytest.raises(DeferredToolSerializationError, match="unsupported"):
        deserialize_history(serialized, schema_version="future-v99")


def test_binary_media_is_never_persisted_in_deferred_history() -> None:
    sentinel = b"private-source-media-sentinel"
    call = ToolCallPart("generate_image", {"prompt": "draw it"}, "call-1")
    history: list[ModelMessage] = [
        ModelRequest(
            parts=[
                UserPromptPart(
                    [
                        "Draw the referenced image",
                        BinaryContent(data=sentinel, media_type="image/png"),
                    ]
                )
            ]
        ),
        ModelResponse(parts=[call]),
    ]

    with pytest.raises(
        DeferredToolSerializationError,
        match="references instead of media",
    ) as raised:
        serialize_deferred_request(call, history)

    assert sentinel.decode() not in str(raised.value)


def test_resume_input_is_built_from_private_stored_state_without_repr_leaks() -> None:
    now = datetime(2026, 7, 20, 12, tzinfo=UTC)
    operation_id = OperationId(uuid4())
    snapshot = DeferredToolSnapshot(
        request_id=uuid4(),
        operation_id=operation_id,
        quote_id=QuoteId.new(),
        requester_id=uuid4(),
        requester_telegram_id=123,
        chat_id=uuid4(),
        chat_telegram_id=-456,
        thread_id=7,
        message_id=8,
        tool_name="generate_image",
        tool_call_id="call-1",
        status=DeferredToolStatus.APPROVED,
        expires_at=now + timedelta(minutes=5),
        decided_at=now,
        resumed_at=now,
        created_at=now,
        updated_at=now,
    )
    call = ToolCallPart(
        snapshot.tool_name,
        {"prompt": "private prompt"},
        snapshot.tool_call_id,
    )
    history = tuple(_history(call))
    lease = ResumeLease(
        snapshot=snapshot,
        claimed_at=now,
        lease_expires_at=now + timedelta(minutes=1),
        _validated_arguments={"prompt": "private prompt"},
        _original_history=history,
    )

    run_input = lease.build_run_input()

    approval = run_input.deferred_tool_results.approvals["call-1"]
    assert isinstance(approval, ToolApproved)
    assert run_input.message_history == history
    assert "private prompt" not in repr(lease)
    assert "private prompt" not in repr(run_input)
