"""Project complete Pydantic AI tool exchanges into application history DTOs."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

from pydantic_ai import ModelMessage, ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.messages import ToolReturnPart

from derp.history.core import ToolCall, ToolResult, ToolRound


def extract_tool_rounds(messages: Sequence[ModelMessage]) -> tuple[ToolRound, ...]:
    """Return complete call/result pairs and discard interrupted partial rounds."""
    returns = {
        part.tool_call_id: part
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    }
    rounds: list[ToolRound] = []
    seen_call_ids: set[str] = set()
    for message in messages:
        if not isinstance(message, ModelResponse):
            continue
        parts = tuple(part for part in message.parts if isinstance(part, ToolCallPart))
        if not parts or any(part.tool_call_id in seen_call_ids for part in parts):
            continue
        matching = tuple(returns.get(part.tool_call_id) for part in parts)
        if any(part is None for part in matching):
            continue
        results = tuple(part for part in matching if part is not None)
        if any(
            call.tool_name != result.tool_name
            for call, result in zip(parts, results, strict=True)
        ):
            continue
        rounds.append(
            ToolRound(
                calls=tuple(_project_call(part) for part in parts),
                results=tuple(_project_result(part) for part in results),
                called_at=message.timestamp,
                returned_at=max(part.timestamp for part in results),
            )
        )
        seen_call_ids.update(part.tool_call_id for part in parts)
    return tuple(rounds)


def serialize_tool_rounds(rounds: Sequence[ToolRound]) -> list[dict[str, object]]:
    """Serialize provider-neutral rounds into canonical JSON-compatible values."""
    return [
        {
            "called_at": _timestamp(round_.called_at),
            "returned_at": _timestamp(round_.returned_at),
            "calls": [
                {
                    "tool_name": call.tool_name,
                    "tool_call_id": call.tool_call_id,
                    "arguments_json": call.arguments_json,
                }
                for call in round_.calls
            ],
            "results": [
                {
                    "tool_name": result.tool_name,
                    "tool_call_id": result.tool_call_id,
                    "content": result.content,
                }
                for result in round_.results
            ],
        }
        for round_ in rounds
    ]


def _project_call(part: ToolCallPart) -> ToolCall:
    arguments = part.args_as_dict(raise_if_invalid=True)
    return ToolCall(
        tool_name=part.tool_name,
        tool_call_id=part.tool_call_id,
        arguments_json=json.dumps(
            arguments,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
    )


def _project_result(part: ToolReturnPart) -> ToolResult:
    return ToolResult(
        tool_name=part.tool_name,
        tool_call_id=part.tool_call_id,
        content=part.model_response_str(),
    )


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


__all__ = ["extract_tool_rounds", "serialize_tool_rounds"]
