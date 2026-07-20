"""Focused tests for the pure conversation history core."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest
from pydantic_ai import (
    BinaryContent,
    FilePart,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from derp.history.core import (
    AssistantTextTurn,
    AttachmentReference,
    LogicalTurn,
    Speaker,
    TokenEstimator,
    ToolCall,
    ToolResult,
    ToolRound,
    UserTextTurn,
    materialize_history,
    trim_history,
)

NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)
USER = Speaker(id=101, display_name="Ada")
OTHER_USER = Speaker(id=202, display_name="Grace")
ASSISTANT = Speaker(id=303, display_name="Derp")


def _user_turn(
    message_id: int,
    text: str,
    *,
    speaker: Speaker = USER,
    attachments: tuple[AttachmentReference, ...] = (),
) -> UserTextTurn:
    return UserTextTurn(
        source_message_id=message_id,
        timestamp=NOW + timedelta(seconds=message_id),
        speaker=speaker,
        text=text,
        attachments=attachments,
    )


def _assistant_turn(
    message_id: int,
    text: str,
    *,
    attachments: tuple[AttachmentReference, ...] = (),
) -> AssistantTextTurn:
    return AssistantTextTurn(
        source_message_id=message_id,
        timestamp=NOW + timedelta(seconds=message_id),
        speaker=ASSISTANT,
        text=text,
        attachments=attachments,
    )


def test_history_dtos_are_frozen_and_reject_ambiguous_source_data() -> None:
    attachment = AttachmentReference(
        media_type="photo",
        file_id="telegram-file",
        file_unique_id="stable-file",
    )
    request = _user_turn(1, "What is this?", attachments=(attachment,))

    assert request.source_message_id == 1
    assert request.speaker is USER
    assert request.attachments == (attachment,)
    with pytest.raises(FrozenInstanceError):
        request.text = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        attachment.file_id = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError, match="timezone-aware"):
        UserTextTurn(
            source_message_id=2,
            timestamp=datetime(2026, 7, 20),
            speaker=USER,
            text="naive timestamp",
        )


def test_materializes_ambient_and_tool_turns_as_native_messages() -> None:
    ambient_ada = LogicalTurn(request=_user_turn(1, "First", speaker=USER))
    ambient_grace = LogicalTurn(request=_user_turn(2, "Second", speaker=OTHER_USER))
    tool_round = ToolRound(
        calls=(
            ToolCall(
                tool_name="lookup",
                tool_call_id="call-1",
                arguments_json='{"query":"weather"}',
            ),
        ),
        results=(
            ToolResult(
                tool_name="lookup",
                tool_call_id="call-1",
                content="sunny",
            ),
        ),
        called_at=NOW + timedelta(seconds=4),
        returned_at=NOW + timedelta(seconds=5),
    )
    exchange = LogicalTurn(
        request=_user_turn(3, "How is the weather?"),
        response=_assistant_turn(6, "It is sunny."),
        tool_rounds=(tool_round,),
    )

    messages = materialize_history((ambient_ada, ambient_grace, exchange))

    assert len(messages) == 6
    assert all(isinstance(message, ModelRequest) for message in messages[:3])
    first_prompt = messages[0].parts[0]
    assert isinstance(first_prompt, UserPromptPart)
    assert first_prompt.content == "First"
    assert messages[0].metadata == {
        "derp.history": {
            "role": "user",
            "source_message_id": 1,
            "speaker": {"id": 101, "display_name": "Ada"},
            "attachments": [],
        }
    }
    assert messages[1].metadata["derp.history"]["speaker"] == {  # type: ignore[index]
        "id": 202,
        "display_name": "Grace",
    }

    call_message = messages[3]
    assert isinstance(call_message, ModelResponse)
    assert call_message.timestamp == tool_round.called_at
    call_part = call_message.parts[0]
    assert isinstance(call_part, ToolCallPart)
    assert call_part.tool_name == "lookup"
    assert call_part.tool_call_id == "call-1"
    assert call_part.args == '{"query":"weather"}'

    result_message = messages[4]
    assert isinstance(result_message, ModelRequest)
    result_part = result_message.parts[0]
    assert isinstance(result_part, ToolReturnPart)
    assert result_part.tool_call_id == call_part.tool_call_id
    assert result_part.content == "sunny"

    response_message = messages[5]
    assert isinstance(response_message, ModelResponse)
    assert response_message.metadata["derp.history"]["source_message_id"] == 6  # type: ignore[index]
    assert response_message.parts == [TextPart("It is sunny.")]


def test_materialization_uses_only_successfully_hydrated_attachments() -> None:
    user_image = AttachmentReference("photo", "user-file", "user-stable")
    missing_image = AttachmentReference("photo", "missing-file", "missing-stable")
    assistant_image = AttachmentReference(
        "generated_image",
        "assistant-file",
        "assistant-stable",
    )
    user_content = BinaryContent(data=b"user", media_type="image/png")
    assistant_content = BinaryContent(data=b"assistant", media_type="image/png")
    turn = LogicalTurn(
        request=_user_turn(
            1,
            "Inspect these",
            attachments=(user_image, missing_image),
        ),
        response=_assistant_turn(
            2,
            "Generated result",
            attachments=(assistant_image,),
        ),
    )

    request, response = materialize_history(
        (turn,),
        hydrated_attachments={
            user_image: user_content,
            assistant_image: assistant_content,
        },
    )

    assert isinstance(request, ModelRequest)
    prompt = request.parts[0]
    assert isinstance(prompt, UserPromptPart)
    assert prompt.content == ["Inspect these", user_content]
    assert isinstance(response, ModelResponse)
    assert response.parts == [
        TextPart("Generated result"),
        FilePart(content=assistant_content),
    ]


def test_token_estimation_is_deterministic_and_charges_for_attachments() -> None:
    estimator = TokenEstimator(
        bytes_per_token=4,
        message_overhead=0,
        part_overhead=0,
        attachment_tokens=10,
    )
    attachment = AttachmentReference("photo", "file", "stable")
    turn = LogicalTurn(
        request=_user_turn(1, "abcd", attachments=(attachment,)),
        response=_assistant_turn(2, "12345"),
    )

    assert estimator.estimate_text("abcd") == 1
    assert estimator.estimate_text("12345") == 2
    assert estimator.estimate_text("🙂") == 1
    assert estimator.estimate_turn(turn) == 13
    assert estimator.estimate_history((turn, turn)) == 26
    assert estimator.estimate_history((turn, turn)) == 26


def test_trim_keeps_newest_contiguous_complete_turns_under_both_budgets() -> None:
    estimator = TokenEstimator(
        bytes_per_token=1,
        message_overhead=0,
        part_overhead=0,
        attachment_tokens=0,
    )
    old = LogicalTurn(request=_user_turn(1, "123456"))
    middle = LogicalTurn(request=_user_turn(2, "1234"))
    newest = LogicalTurn(request=_user_turn(3, "5678"))

    assert trim_history(
        (old, middle, newest),
        max_turns=2,
        max_tokens=8,
        estimator=estimator,
    ) == (middle, newest)
    assert trim_history(
        (old, middle, newest),
        max_turns=1,
        max_tokens=100,
        estimator=estimator,
    ) == (newest,)


def test_trim_never_splits_exchange_or_skips_an_oversized_newest_turn() -> None:
    estimator = TokenEstimator(
        bytes_per_token=1,
        message_overhead=0,
        part_overhead=0,
        attachment_tokens=0,
    )
    small = LogicalTurn(request=_user_turn(1, "x"))
    tool_round = ToolRound(
        calls=(ToolCall("t", "c", "{}"),),
        results=(ToolResult("t", "c", "r"),),
        called_at=NOW + timedelta(seconds=3),
        returned_at=NOW + timedelta(seconds=4),
    )
    complete = LogicalTurn(
        request=_user_turn(2, "q"),
        response=_assistant_turn(5, "a"),
        tool_rounds=(tool_round,),
    )
    complete_cost = estimator.estimate_turn(complete)

    retained = trim_history(
        (small, complete),
        max_turns=10,
        max_tokens=complete_cost,
        estimator=estimator,
    )

    assert retained == (complete,)
    assert len(materialize_history(retained)) == 4
    oversized = LogicalTurn(request=_user_turn(6, "too-large"))
    assert (
        trim_history(
            (small, oversized),
            max_turns=10,
            max_tokens=5,
            estimator=estimator,
        )
        == ()
    )


def test_tool_round_rejects_missing_or_mismatched_results() -> None:
    call = ToolCall("lookup", "call-1", "{}")

    with pytest.raises(ValueError, match="exactly one matching result"):
        ToolRound(
            calls=(call,),
            results=(ToolResult("lookup", "call-2", "result"),),
            called_at=NOW,
            returned_at=NOW,
        )
    with pytest.raises(ValueError, match="same name"):
        ToolRound(
            calls=(call,),
            results=(ToolResult("other", "call-1", "result"),),
            called_at=NOW,
            returned_at=NOW,
        )
    with pytest.raises(ValueError, match="final assistant response"):
        LogicalTurn(
            request=_user_turn(1, "question"),
            tool_rounds=(
                ToolRound(
                    calls=(call,),
                    results=(ToolResult("lookup", "call-1", "result"),),
                    called_at=NOW,
                    returned_at=NOW,
                ),
            ),
        )
