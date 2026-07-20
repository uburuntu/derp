"""Pure application history types, trimming, and Pydantic AI materialization."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic_ai import (
    BinaryContent,
    FilePart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelResponsePart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserContent,
    UserPromptPart,
)


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class Speaker:
    """Stable identity and display label for one history participant."""

    id: int | None
    display_name: str

    def __post_init__(self) -> None:
        _require_text(self.display_name, "display_name")


@dataclass(frozen=True, slots=True)
class AttachmentReference:
    """Durable attachment identity without media bytes or a signed URL."""

    media_type: str
    file_id: str
    file_unique_id: str

    def __post_init__(self) -> None:
        _require_text(self.media_type, "media_type")
        _require_text(self.file_id, "file_id")
        _require_text(self.file_unique_id, "file_unique_id")


@dataclass(frozen=True, slots=True)
class UserTextTurn:
    """One source user message retained as application history."""

    source_message_id: int
    timestamp: datetime
    speaker: Speaker
    text: str
    attachments: tuple[AttachmentReference, ...] = ()

    def __post_init__(self) -> None:
        if self.source_message_id <= 0:
            raise ValueError("source_message_id must be positive")
        _require_aware(self.timestamp, "timestamp")
        _require_text(self.text, "text")


@dataclass(frozen=True, slots=True)
class AssistantTextTurn:
    """One source assistant message retained as application history."""

    source_message_id: int
    timestamp: datetime
    speaker: Speaker
    text: str
    attachments: tuple[AttachmentReference, ...] = ()

    def __post_init__(self) -> None:
        if self.source_message_id <= 0:
            raise ValueError("source_message_id must be positive")
        _require_aware(self.timestamp, "timestamp")
        _require_text(self.text, "text")


@dataclass(frozen=True, slots=True)
class ToolCall:
    """Provider-neutral tool call retained inside a logical turn."""

    tool_name: str
    tool_call_id: str
    arguments_json: str

    def __post_init__(self) -> None:
        _require_text(self.tool_name, "tool_name")
        _require_text(self.tool_call_id, "tool_call_id")
        try:
            arguments = json.loads(self.arguments_json)
        except json.JSONDecodeError as exc:
            raise ValueError("arguments_json must contain valid JSON") from exc
        if not isinstance(arguments, dict):
            raise ValueError("arguments_json must contain a JSON object")


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Result paired with exactly one retained tool call."""

    tool_name: str
    tool_call_id: str
    content: str

    def __post_init__(self) -> None:
        _require_text(self.tool_name, "tool_name")
        _require_text(self.tool_call_id, "tool_call_id")
        _require_text(self.content, "content")


@dataclass(frozen=True, slots=True)
class ToolRound:
    """One model tool-call response and its complete application result request."""

    calls: tuple[ToolCall, ...]
    results: tuple[ToolResult, ...]
    called_at: datetime
    returned_at: datetime

    def __post_init__(self) -> None:
        if not self.calls:
            raise ValueError("a tool round requires at least one call")
        if not self.results:
            raise ValueError("a tool round requires at least one result")
        _require_aware(self.called_at, "called_at")
        _require_aware(self.returned_at, "returned_at")
        if self.returned_at < self.called_at:
            raise ValueError("returned_at must not precede called_at")

        calls_by_id = {call.tool_call_id: call for call in self.calls}
        if len(calls_by_id) != len(self.calls):
            raise ValueError("tool call IDs must be unique within a round")
        results_by_id = {result.tool_call_id: result for result in self.results}
        if len(results_by_id) != len(self.results):
            raise ValueError("tool result IDs must be unique within a round")
        if calls_by_id.keys() != results_by_id.keys():
            raise ValueError("every tool call must have exactly one matching result")
        if any(
            call.tool_name != results_by_id[call_id].tool_name
            for call_id, call in calls_by_id.items()
        ):
            raise ValueError("paired tool calls and results must use the same name")


@dataclass(frozen=True, slots=True)
class LogicalTurn:
    """Atomic history entry: an ambient request or one complete assistant exchange."""

    request: UserTextTurn
    response: AssistantTextTurn | None = None
    tool_rounds: tuple[ToolRound, ...] = ()

    def __post_init__(self) -> None:
        if self.tool_rounds and self.response is None:
            raise ValueError("tool rounds require a final assistant response")


@dataclass(frozen=True, slots=True)
class TokenEstimator:
    """Deterministic UTF-8 size heuristic used for history window policy."""

    bytes_per_token: int = 4
    message_overhead: int = 4
    part_overhead: int = 1
    attachment_tokens: int = 256

    def __post_init__(self) -> None:
        if self.bytes_per_token <= 0:
            raise ValueError("bytes_per_token must be positive")
        if (
            min(
                self.message_overhead,
                self.part_overhead,
                self.attachment_tokens,
            )
            < 0
        ):
            raise ValueError("token overheads must not be negative")

    def estimate_text(self, text: str) -> int:
        """Estimate text tokens from its UTF-8 byte length, rounding up."""
        byte_count = len(text.encode("utf-8"))
        return (byte_count + self.bytes_per_token - 1) // self.bytes_per_token

    def estimate_turn(self, turn: LogicalTurn) -> int:
        """Estimate all model-visible messages in one atomic logical turn."""
        request = turn.request
        total = self.message_overhead + self._estimate_part(request.text)
        total += len(request.attachments) * (
            self.part_overhead + self.attachment_tokens
        )

        for round_ in turn.tool_rounds:
            total += self.message_overhead
            total += sum(
                self._estimate_part(
                    call.tool_name,
                    call.tool_call_id,
                    call.arguments_json,
                )
                for call in round_.calls
            )
            total += self.message_overhead
            total += sum(
                self._estimate_part(
                    result.tool_name,
                    result.tool_call_id,
                    result.content,
                )
                for result in round_.results
            )

        if response := turn.response:
            total += self.message_overhead + self._estimate_part(response.text)
            total += len(response.attachments) * (
                self.part_overhead + self.attachment_tokens
            )
        return total

    def estimate_history(self, turns: Sequence[LogicalTurn]) -> int:
        """Estimate a sequence by summing its atomic turn estimates."""
        return sum(self.estimate_turn(turn) for turn in turns)

    def _estimate_part(self, *values: str) -> int:
        return self.part_overhead + sum(self.estimate_text(value) for value in values)


DEFAULT_TOKEN_ESTIMATOR = TokenEstimator()


def trim_history(
    turns: Sequence[LogicalTurn],
    *,
    max_turns: int,
    max_tokens: int,
    estimator: TokenEstimator = DEFAULT_TOKEN_ESTIMATOR,
) -> tuple[LogicalTurn, ...]:
    """Keep the newest contiguous logical turns that fit both history budgets."""
    if max_turns < 0:
        raise ValueError("max_turns must not be negative")
    if max_tokens < 0:
        raise ValueError("max_tokens must not be negative")

    retained_newest_first: list[LogicalTurn] = []
    token_count = 0
    for turn in reversed(turns):
        if len(retained_newest_first) == max_turns:
            break
        turn_tokens = estimator.estimate_turn(turn)
        if token_count + turn_tokens > max_tokens:
            break
        retained_newest_first.append(turn)
        token_count += turn_tokens
    return tuple(reversed(retained_newest_first))


def materialize_history(
    turns: Sequence[LogicalTurn],
    *,
    hydrated_attachments: Mapping[AttachmentReference, BinaryContent] | None = None,
) -> tuple[ModelMessage, ...]:
    """Convert application turns to native Pydantic AI 2.13 messages.

    Durable attachment references are never emitted as model content. Callers may
    pass successfully hydrated binary values; missing values leave the text turn
    intact for graceful media degradation.
    """
    messages: list[ModelMessage] = []
    for turn in turns:
        request_files = _hydrated_files(
            turn.request.attachments,
            hydrated_attachments,
        )
        request_content: str | list[UserContent] = turn.request.text
        if request_files:
            request_content = [turn.request.text, *request_files]
        messages.append(
            ModelRequest(
                parts=[
                    UserPromptPart(
                        content=request_content,
                        timestamp=turn.request.timestamp,
                    )
                ],
                timestamp=turn.request.timestamp,
                metadata=_source_metadata(turn.request, role="user"),
            )
        )

        for round_ in turn.tool_rounds:
            messages.append(
                ModelResponse(
                    parts=[
                        ToolCallPart(
                            tool_name=call.tool_name,
                            tool_call_id=call.tool_call_id,
                            args=call.arguments_json,
                        )
                        for call in round_.calls
                    ],
                    timestamp=round_.called_at,
                )
            )
            messages.append(
                ModelRequest(
                    parts=[
                        ToolReturnPart(
                            tool_name=result.tool_name,
                            tool_call_id=result.tool_call_id,
                            content=result.content,
                            timestamp=round_.returned_at,
                        )
                        for result in round_.results
                    ],
                    timestamp=round_.returned_at,
                )
            )

        if response := turn.response:
            response_parts: list[ModelResponsePart] = [TextPart(response.text)]
            response_parts.extend(
                FilePart(content=content)
                for content in _hydrated_files(
                    response.attachments,
                    hydrated_attachments,
                )
            )
            messages.append(
                ModelResponse(
                    parts=response_parts,
                    timestamp=response.timestamp,
                    metadata=_source_metadata(response, role="assistant"),
                )
            )
    return tuple(messages)


def _hydrated_files(
    references: Sequence[AttachmentReference],
    hydrated: Mapping[AttachmentReference, BinaryContent] | None,
) -> list[BinaryContent]:
    if hydrated is None:
        return []
    return [hydrated[reference] for reference in references if reference in hydrated]


def _source_metadata(
    turn: UserTextTurn | AssistantTextTurn,
    *,
    role: str,
) -> dict[str, object]:
    return {
        "derp.history": {
            "role": role,
            "source_message_id": turn.source_message_id,
            "speaker": {
                "id": turn.speaker.id,
                "display_name": turn.speaker.display_name,
            },
            "attachments": [
                {
                    "media_type": attachment.media_type,
                    "file_id": attachment.file_id,
                    "file_unique_id": attachment.file_unique_id,
                }
                for attachment in turn.attachments
            ],
        }
    }


__all__ = [
    "DEFAULT_TOKEN_ESTIMATOR",
    "AssistantTextTurn",
    "AttachmentReference",
    "LogicalTurn",
    "Speaker",
    "TokenEstimator",
    "ToolCall",
    "ToolResult",
    "ToolRound",
    "UserTextTurn",
    "materialize_history",
    "trim_history",
]
