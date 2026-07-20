"""Trusted Pydantic AI history and validated tool-call persistence."""

from __future__ import annotations

import json
from collections.abc import Sequence
from copy import deepcopy
from typing import Final, cast

from pydantic_ai import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelResponse,
    ToolCallPart,
)
from pydantic_core import to_jsonable_python

HISTORY_SCHEMA_VERSION: Final = "pydantic-ai-model-messages-v1"
MAX_DEFERRED_ARGUMENT_BYTES: Final = 64 * 1024
MAX_DEFERRED_HISTORY_BYTES: Final = 2 * 1024 * 1024
_NON_DURABLE_MEDIA_KINDS: Final = frozenset(
    {
        "audio-url",
        "binary",
        "document-url",
        "image-url",
        "uploaded-file",
        "video-url",
    }
)


class DeferredToolSerializationError(ValueError):
    """Trusted deferred state cannot be represented by the durable schema."""


def validate_tool_call_identity(tool_call: ToolCallPart) -> None:
    """Validate the immutable, content-free identity stored beside a tool call."""
    if not isinstance(tool_call, ToolCallPart):
        raise DeferredToolSerializationError("tool_call must be a ToolCallPart")
    if not tool_call.tool_name.strip() or len(tool_call.tool_name) > 64:
        raise DeferredToolSerializationError("tool name is invalid")
    if not tool_call.tool_call_id.strip() or len(tool_call.tool_call_id) > 255:
        raise DeferredToolSerializationError("tool call ID is invalid")


def serialize_deferred_request(
    tool_call: ToolCallPart,
    original_history: Sequence[ModelMessage],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Serialize one validated call and its exact trusted run history."""
    validate_tool_call_identity(tool_call)
    if not original_history:
        raise DeferredToolSerializationError("original history must not be empty")

    try:
        arguments = to_jsonable_python(
            tool_call.args_as_dict(raise_if_invalid=True),
            bytes_mode="base64",
        )
        serialized_history = to_jsonable_python(
            list(original_history),
            bytes_mode="base64",
        )
        if not isinstance(arguments, dict):
            raise TypeError
        if not isinstance(serialized_history, list) or not all(
            isinstance(message, dict) for message in serialized_history
        ):
            raise TypeError
        if _contains_non_durable_media(serialized_history):
            raise DeferredToolSerializationError(
                "deferred history must contain references instead of media"
            )
        if _json_size(arguments) > MAX_DEFERRED_ARGUMENT_BYTES:
            raise DeferredToolSerializationError(
                "deferred tool arguments exceed the persistence limit"
            )
        if _json_size(serialized_history) > MAX_DEFERRED_HISTORY_BYTES:
            raise DeferredToolSerializationError(
                "deferred history exceeds the persistence limit"
            )
        restored = ModelMessagesTypeAdapter.validate_python(serialized_history)
    except DeferredToolSerializationError:
        raise
    except Exception:
        raise DeferredToolSerializationError(
            "deferred tool state is not valid JSON history"
        ) from None

    if not restored or not isinstance(restored[-1], ModelResponse):
        raise DeferredToolSerializationError(
            "deferred tool call must be in the final model response"
        )
    matching_calls = [
        part
        for part in restored[-1].parts
        if isinstance(part, ToolCallPart)
        and part.tool_call_id == tool_call.tool_call_id
    ]
    if len(matching_calls) != 1:
        raise DeferredToolSerializationError(
            "deferred tool call is missing or ambiguous in original history"
        )
    persisted_call = matching_calls[0]
    try:
        persisted_arguments = to_jsonable_python(
            persisted_call.args_as_dict(raise_if_invalid=True),
            bytes_mode="base64",
        )
    except Exception:
        raise DeferredToolSerializationError(
            "deferred tool history contains invalid arguments"
        ) from None
    if (
        persisted_call.tool_name != tool_call.tool_name
        or persisted_arguments != arguments
    ):
        raise DeferredToolSerializationError(
            "deferred tool call does not match original history"
        )

    return (
        cast(dict[str, object], deepcopy(arguments)),
        cast(list[dict[str, object]], deepcopy(serialized_history)),
    )


def _contains_non_durable_media(value: object) -> bool:
    if isinstance(value, dict):
        if value.get("kind") in _NON_DURABLE_MEDIA_KINDS:
            return True
        return any(_contains_non_durable_media(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_non_durable_media(item) for item in value)
    return False


def _json_size(value: object) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
    )


def deserialize_history(
    serialized_history: list[dict[str, object]],
    *,
    schema_version: str,
) -> tuple[ModelMessage, ...]:
    """Rebuild native messages only through Pydantic AI's versioned adapter."""
    if schema_version != HISTORY_SCHEMA_VERSION:
        raise DeferredToolSerializationError("unsupported history schema version")
    try:
        return tuple(
            ModelMessagesTypeAdapter.validate_python(deepcopy(serialized_history))
        )
    except Exception:
        raise DeferredToolSerializationError(
            "persisted deferred history is invalid"
        ) from None


__all__ = [
    "HISTORY_SCHEMA_VERSION",
    "MAX_DEFERRED_ARGUMENT_BYTES",
    "MAX_DEFERRED_HISTORY_BYTES",
    "DeferredToolSerializationError",
    "deserialize_history",
    "serialize_deferred_request",
    "validate_tool_call_identity",
]
