from decimal import Decimal

import pytest
from pydantic_ai.messages import (
    BinaryContent,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage
from sqlalchemy import func, select

from derp.agent.models import AgentRun
from derp.agent.runs import (
    finish_run,
    load_messages,
    record_output,
    run_for_output,
    serialize_messages,
    start_run,
)
from derp.db.engine import Database
from derp.inference.models import ProviderCall


def history() -> list[ModelRequest | ModelResponse]:
    return [
        ModelRequest(
            parts=[UserPromptPart(["look", BinaryContent(data=b"x" * 100, media_type="image/png")])]
        ),
        ModelResponse(
            parts=[ToolCallPart("search_history", {"query": "cats"}, tool_call_id="t1")],
            usage=RequestUsage(input_tokens=500, output_tokens=20),
            model_name="openai/gpt-6-luna",
            provider_details={"cost": 0.0003, "downstream_provider": "Azure"},
        ),
        ModelRequest(parts=[ToolReturnPart("search_history", "none", tool_call_id="t1")]),
        ModelResponse(
            parts=[TextPart("no cats")],
            usage=RequestUsage(input_tokens=600, output_tokens=5),
            model_name="openai/gpt-6-luna",
            provider_details={"cost": 0.0002},
        ),
    ]


def test_serialized_history_drops_binary_and_round_trips() -> None:
    data = serialize_messages(history())
    assert "eHh4" not in str(data)  # base64 of the image bytes
    assert "[image/png omitted]" in str(data)
    assert len(load_messages(data)) == 4


@pytest.mark.db
async def test_run_lifecycle_records_usage_and_outputs(database: Database) -> None:
    async with database.transaction() as session:
        run = await start_run(
            session,
            chat_id=-100,
            user_id=7,
            trigger="message",
            role="chat",
            funding="paid",
            trigger_message_id=3,
        )
        result = finish_run(session, run, history(), status="completed", context_tokens=900)
        record_output(session, run, 11, "message")
        record_output(session, run, 3, "reaction")
    assert result.cost_usd == Decimal("0.0005")
    assert result.tool_calls == 1
    async with database.session() as session:
        stored = await run_for_output(session, -100, 11)
        assert stored is not None and stored.id == run.id
        assert stored.served_model == "openai/gpt-6-luna"
        assert (stored.input_tokens, stored.output_tokens, stored.context_tokens) == (1100, 25, 900)
        assert await run_for_output(session, -100, 3) is None  # reactions are not messages
        calls = await session.scalar(
            select(func.count()).select_from(ProviderCall).where(ProviderCall.run_id == run.id)
        )
        assert calls == 2
        assert await session.scalar(select(AgentRun.status).where(AgentRun.id == run.id)) == "completed"
