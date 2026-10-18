import pytest

"""Runs end in an answer: caps wrap up instead of failing, empty replies and busy providers get a retry."""

from decimal import Decimal

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.native_tools import WebFetchTool, WebSearchTool
from sqlalchemy import select

from derp.agent.guard import EMPTY_NOTE, SEARCH_NOTE, WRAP_UP_NOTE
from derp.agent.models import AgentRun
from derp.config import RunCaps, Settings
from derp.history.models import User
from derp.inference.models import ProviderCall
from derp.inference.roles import DEFAULT_PINS, ModelRole
from derp.money.free import POLICY_VERSION
from derp.money.wallets import balance, credit, wallet
from tests.e2e.harness import Conversation, Person
from tests.e2e.model import Responder, ScriptedModel, thinking_only

DORA = Person(701, "Dora")
OWNER = Person(1000, "Owner")  # gets the sponsored-cap ping


def search(n: int) -> Responder:
    def respond(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[ToolCallPart("search_history", {"queries": [f"q{n}"]}, tool_call_id=f"s{n}")]
        )

    return respond


def busy(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
    raise ModelHTTPError(status_code=429, model_name="free")


def notes(messages: list[ModelMessage]) -> list[str]:
    return [
        str(part.content)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart | RetryPromptPart)
    ]


def results(messages: list[ModelMessage]) -> int:
    return sum(
        isinstance(part, ToolReturnPart) and part.tool_name == "search_history"
        for message in messages
        for part in message.parts
    )


async def cap_tools(conversation: Conversation, calls: int) -> None:
    conversation.settings = conversation.settings.model_copy(update={"run_caps": RunCaps(tool_calls=calls)})
    await conversation.restart()


async def test_reaching_the_tool_cap_ends_in_an_answer(
    conversation: Conversation, model: ScriptedModel
) -> None:
    await cap_tools(conversation, 2)
    model.reply(search(1), search(2), "found what I could")
    sent = await conversation.send(DORA, text="dig through everything")
    reply = await conversation.expect_reply(sent)
    assert reply["rich_message"]["markdown"] == "found what I could"
    await conversation.settle(sent)
    assert [info.model_settings and info.model_settings.get("tool_choice") for info in model.infos] == [
        None,
        None,
        "none",
    ]
    assert WRAP_UP_NOTE in notes(model.requests[-1])
    assert model.infos[-1].function_tools  # schemas stay, so the cached prefix still matches
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun))
        assert run is not None and run.status == "completed"
        assert WRAP_UP_NOTE not in str(run.messages)  # the note is request-only


async def test_a_tool_called_past_the_cap_is_not_run(
    conversation: Conversation, model: ScriptedModel
) -> None:
    await cap_tools(conversation, 1)
    model.reply(search(1), search(2), "that's all")
    sent = await conversation.send(DORA, text="dig")
    assert (await conversation.expect_reply(sent))["rich_message"]["markdown"] == "that's all"
    await conversation.settle(sent)
    assert results(model.requests[-1]) == 1
    assert any(WRAP_UP_NOTE in note for note in notes(model.requests[-1]))


async def test_the_money_hold_ends_in_an_answer(conversation: Conversation, model: ScriptedModel) -> None:
    async with conversation.db.transaction() as session:
        wallet_id = (await wallet(session, "user", DORA.id)).id
        await credit(session, wallet_id, "stars:dora", 12, "purchase")
    model.cost = 0.004  # 5 credits a turn: after two, one more like them would reach the 12 held
    model.reply(search(1), search(2), "here's the gist")
    sent = await conversation.send(DORA, text="find it all")
    reply = await conversation.expect_reply(sent)
    assert reply["rich_message"]["markdown"] == "here's the gist"
    await conversation.settle(sent)
    assert model.infos[-1].model_settings and model.infos[-1].model_settings.get("tool_choice") == "none"
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun))
        assert run is not None and (run.status, run.charged_credits) == (
            "completed",
            12,
        )  # never past the hold
        assert await balance(session, wallet_id) == 0


async def test_an_answer_cut_off_while_thinking_is_retried_on_the_fallback(
    conversation: Conversation, model: ScriptedModel
) -> None:
    model.reply(thinking_only("length"), "there you go")
    sent = await conversation.send(DORA, text="explain everything")
    reply = await conversation.expect_reply(sent)
    assert reply["rich_message"]["markdown"] == "there you go"
    await conversation.settle(sent)
    pin = DEFAULT_PINS[ModelRole.CHAT_SPONSORED]
    assert model.pins[-1] == pin.rotated()
    retry = model.infos[-1].model_settings or {}
    assert retry.get("max_tokens") == 2 * conversation.settings.run_caps.output_tokens
    assert retry.get("openrouter_models") == list(pin.rotated().fallbacks)
    assert EMPTY_NOTE in notes(model.requests[-1])
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun))
        assert run is not None and run.status == "completed"


async def test_an_empty_reply_is_retried_once(conversation: Conversation, model: ScriptedModel) -> None:
    model.reply(thinking_only(), thinking_only(), "finally")
    sent = await conversation.send(DORA, text="hello?")
    reply = await conversation.expect_reply(sent)
    assert reply["rich_message"]["markdown"] == "finally"  # then Pydantic AI's own retries take over
    assert (model.infos[1].model_settings or {}).get(
        "max_tokens"
    ) == conversation.settings.run_caps.output_tokens
    await conversation.settle(sent)


async def consent_to_free(conversation: Conversation, settings: Settings) -> None:
    conversation.meet(OWNER)
    async with conversation.db.transaction() as session:
        cap = Decimal(str(settings.sponsored_global_daily_usd))
        session.add(ProviderCall(kind="chat", model="m", funding="sponsored", cost_usd=cap))
        session.add(User(id=DORA.id, first_name="Dora", free_models_consent_version=POLICY_VERSION))


async def test_a_busy_free_model_falls_back_to_the_next(
    conversation: Conversation, model: ScriptedModel, settings: Settings
) -> None:
    await consent_to_free(conversation, settings)
    model.reply(busy, "free answer")
    sent = await conversation.send(DORA, text="hi")
    assert (await conversation.expect_reply(sent))["rich_message"]["markdown"] == "free answer"
    await conversation.settle(sent)
    pin = DEFAULT_PINS[ModelRole.CHAT_FREE]
    assert model.pins[-1] == pin.rotated()
    assert (model.infos[-1].model_settings or {}).get("openrouter_models") == list(pin.rotated().fallbacks)
    async with conversation.db.session() as session:
        run = await session.scalar(select(AgentRun))
        assert run is not None and (run.status, run.funding) == ("completed", "free")


def searches_allowed(info: AgentInfo) -> int | None:
    """The request's web search allowance; None when search was left out."""
    tool = next((t for t in info.model_request_parameters.native_tools if isinstance(t, WebSearchTool)), None)
    return None if tool is None else tool.max_uses


async def test_web_search_is_left_out_when_the_hold_cant_cover_it(
    conversation: Conversation, model: ScriptedModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sponsored: the day's free Derpcoins hold less than one (pricier) search's fee plus markup.
    monkeypatch.setattr(conversation.settings, "web_search_price_usd", 0.01)
    sent = await conversation.send(DORA, text="derp what's new in the news")
    await conversation.expect_reply(sent)
    await conversation.settle(sent)
    assert searches_allowed(model.infos[-1]) is None
    assert any(isinstance(t, WebFetchTool) for t in model.infos[-1].model_request_parameters.native_tools)
    assert SEARCH_NOTE in notes(model.requests[-1])


async def test_web_searches_are_limited_to_what_the_hold_covers(
    conversation: Conversation, model: ScriptedModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(conversation.settings, "web_search_price_usd", 0.01)  # 12 credits a search
    async with conversation.db.transaction() as session:
        wallet_id = (await wallet(session, "user", DORA.id)).id
        await credit(session, wallet_id, "stars:dora", 20, "purchase")
    sent = await conversation.send(DORA, text="derp look it up")
    await conversation.expect_reply(sent)
    await conversation.settle(sent)
    assert searches_allowed(model.infos[-1]) == 1  # 20 held: one turn, then one 12-credit search fits
    assert SEARCH_NOTE not in notes(model.requests[-1])

    async with conversation.db.transaction() as session:
        await credit(session, wallet_id, "stars:dora-more", 500, "purchase")
    sent = await conversation.send(DORA, text="derp look it up again")
    await conversation.expect_reply(sent)
    await conversation.settle(sent)
    assert searches_allowed(model.infos[-1]) == 2
