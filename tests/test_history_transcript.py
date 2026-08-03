"""Pydantic AI tool transcripts retain only complete, canonical exchanges."""

from datetime import UTC, datetime, timedelta

from pydantic_ai import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart

from derp.history.transcript import extract_tool_rounds, serialize_tool_rounds

NOW = datetime(2026, 7, 20, tzinfo=UTC)


def test_extracts_complete_parallel_tool_round_in_stable_order() -> None:
    messages = [
        ModelResponse(
            parts=[
                ToolCallPart("search", {"z": 2, "a": 1}, tool_call_id="call-1"),
                ToolCallPart("think", {"question": "why"}, tool_call_id="call-2"),
            ],
            timestamp=NOW,
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    "search",
                    {"items": ["one", "two"]},
                    tool_call_id="call-1",
                    timestamp=NOW + timedelta(seconds=1),
                ),
                ToolReturnPart(
                    "think",
                    "because",
                    tool_call_id="call-2",
                    timestamp=NOW + timedelta(seconds=2),
                ),
            ],
            timestamp=NOW + timedelta(seconds=2),
        ),
    ]

    rounds = extract_tool_rounds(messages)

    assert len(rounds) == 1
    assert rounds[0].calls[0].arguments_json == '{"a":1,"z":2}'
    assert rounds[0].results[0].content == '{"items":["one","two"]}'
    assert rounds[0].returned_at == NOW + timedelta(seconds=2)
    assert serialize_tool_rounds(rounds)[0]["called_at"] == "2026-07-20T00:00:00Z"


def test_drops_partial_or_name_mismatched_rounds() -> None:
    messages = [
        ModelResponse(
            parts=[ToolCallPart("search", {}, tool_call_id="partial")],
            timestamp=NOW,
        ),
        ModelResponse(
            parts=[ToolCallPart("search", {}, tool_call_id="mismatch")],
            timestamp=NOW,
        ),
        ModelRequest(
            parts=[ToolReturnPart("other", "no", tool_call_id="mismatch")],
            timestamp=NOW,
        ),
    ]

    assert extract_tool_rounds(messages) == ()
