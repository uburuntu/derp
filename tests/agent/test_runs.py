from decimal import Decimal

import httpx
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
    price_unreported,
    record_output,
    run_for_output,
    serialize_messages,
    start_run,
)
from derp.config import Settings
from derp.db.engine import Database
from derp.inference.api import OpenRouterAPI
from derp.inference.models import ModelPin, ProviderCall
from derp.inference.openrouter import response_usage
from derp.inference.roles import ModelRole


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


async def priced(database: Database, prompt: str, completion: str) -> None:
    async with database.transaction() as session:
        for role in (ModelRole.CHAT, ModelRole.CHAT_FREE):
            session.add(
                ModelPin(
                    role=role.value, pin={"model": "a/b"},
                    price_prompt=Decimal(prompt), price_completion=Decimal(completion),
                )
            )  # fmt: skip


def answer(text: str, input_tokens: int = 0, output_tokens: int = 0, **extra: object) -> ModelResponse:
    usage = RequestUsage(input_tokens=input_tokens, output_tokens=output_tokens)
    return ModelResponse(parts=[TextPart(text)], usage=usage, **extra)  # type: ignore[arg-type]


@pytest.mark.db
async def test_paid_responses_without_a_cost_are_estimated_never_free(database: Database) -> None:
    await priced(database, "2", "8")  # USD per 1M tokens
    unreported = answer("done", 1000, 100)
    reported = answer("ok", 10, 1, provider_details={"cost": 0.5})
    cut = answer("half an ans", state="interrupted")  # no usage at all
    await price_unreported(database, [unreported, reported, cut], ModelRole.CHAT, 4000)
    assert response_usage(unreported).cost_usd == Decimal("0.0028")  # 1000 × $2/M + 100 × $8/M
    assert (unreported.provider_details or {}).get("estimated") is True
    assert response_usage(reported).cost_usd == Decimal("0.5")  # a reported cost is kept
    assert response_usage(cut).cost_usd == Decimal("0.008024")  # the context in, the written text out

    free = answer("hi", 1000, 100)
    await price_unreported(database, [free], ModelRole.CHAT_FREE, 4000)
    assert response_usage(free).cost_usd == 0  # free models stay free


@pytest.mark.db
async def test_an_unreported_cost_is_looked_up_before_it_is_estimated(database: Database) -> None:
    await priced(database, "2", "8")

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.params["id"] == "gen-7"
        return httpx.Response(200, json={"data": {"total_cost": 0.0042}})

    settings = Settings(_env_file=None, openrouter_api_key="sk-or-test")  # type: ignore[call-arg]
    client = httpx.AsyncClient(base_url="https://or.test/api/v1/", transport=httpx.MockTransport(handle))
    response = answer("done", 1000, 100, provider_response_id="gen-7")
    await price_unreported(database, [response], ModelRole.CHAT, 0, api=OpenRouterAPI(settings, client))
    assert response_usage(response).cost_usd == Decimal("0.0042")
