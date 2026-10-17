import asyncio
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.models.openrouter import OpenRouterModelSettings
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from derp.agent.deps import RunDeps
from derp.agent.guard import Guard, retryable, unanswered
from derp.inference.openrouter import chat_settings
from derp.inference.roles import RolePin

PIN = RolePin(model="a/primary", fallbacks=("b/second", "c/third"))


def test_a_reply_with_nothing_to_act_on_is_unanswered() -> None:
    assert unanswered(ModelResponse(parts=[]))
    assert unanswered(ModelResponse(parts=[ThinkingPart("hmm")]))
    assert unanswered(ModelResponse(parts=[ThinkingPart("hmm"), TextPart("  ")]))
    assert not unanswered(ModelResponse(parts=[ThinkingPart("hmm"), TextPart("hi")]))
    assert not unanswered(ModelResponse(parts=[ToolCallPart("react", {"emoji": "🔥"})]))
    assert not unanswered(ModelResponse(parts=[TextPart("cut off mid-sent")], finish_reason="length"))
    cut = ToolCallPart("send_poll", '{"question": "best ca', tool_call_id="p")
    assert unanswered(ModelResponse(parts=[ThinkingPart("long"), cut], finish_reason="length"))
    whole = ToolCallPart("react", '{"emoji": "🔥"}', tool_call_id="r")
    assert not unanswered(ModelResponse(parts=[whole], finish_reason="length"))


def test_only_failures_worth_another_try_are_retried() -> None:
    assert retryable(ModelHTTPError(429, "m"))
    assert retryable(ModelHTTPError(502, "m"))
    assert retryable(ModelAPIError("m", "connection reset"))
    assert not retryable(ModelHTTPError(400, "m"))
    assert not retryable(ModelHTTPError(402, "m"))
    assert not retryable(RuntimeError("bug"))


def test_a_rotated_pin_starts_at_its_first_fallback() -> None:
    assert PIN.rotated().models == ("b/second", "c/third", "a/primary")
    lone = RolePin(model="a/primary")
    assert lone.rotated() == lone


def guard(*, deadline: float = 1e12, pin: RolePin = PIN) -> tuple[Guard, list[RolePin]]:
    built: list[RolePin] = []

    def factory(pin: RolePin) -> Model:
        built.append(pin)
        return TestModel()

    made = Guard(run_id=1, pin=pin, model_factory=factory, tool_calls=3, requests=4, deadline=deadline)
    return made, built


def context(*, requests: int = 0, tool_calls: int = 0) -> RunContext[RunDeps]:
    usage = RunUsage(requests=requests, tool_calls=tool_calls)
    deps = SimpleNamespace(funds=SimpleNamespace(reservation_id=None))  # free: no hold to reach
    return cast(RunContext[RunDeps], SimpleNamespace(usage=usage, messages=[], deps=deps))


async def test_the_run_wraps_up_at_any_soft_cap_and_stays_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    fresh, _ = guard()
    assert not await fresh.check(context(requests=3, tool_calls=2))
    assert await fresh.check(context(tool_calls=3))
    assert await fresh.check(context())  # sticky for the rest of the run
    assert await guard()[0].check(context(requests=4))
    late = asyncio.get_running_loop().time() - 1
    assert await guard(deadline=late)[0].check(context())

    async def nothing_left(_ctx: RunContext[RunDeps], *, next_turn: bool = False) -> int:
        assert next_turn  # one more request like the latest must still fit
        return 0

    monkeypatch.setattr("derp.agent.guard.hold_left", nothing_left)
    assert await guard()[0].check(context())


async def test_one_recovery_per_run_moves_it_to_the_rotated_pin() -> None:
    made, built = guard()
    assert made.routed({"max_tokens": 100}) == {"max_tokens": 100}
    assert made.recover("an empty reply")
    assert built == [PIN.rotated()]
    made.longer = True
    settings = OpenRouterModelSettings(max_tokens=100, openrouter_models=["b/second", "c/third"])
    assert made.routed(settings) == {
        "max_tokens": 200,
        "openrouter_models": ["c/third", "a/primary"],
        "openrouter_provider": {"zdr": True, "data_collection": "deny"},
    }
    assert not made.recover("a second failure")
    assert len(built) == 1


def test_the_retry_drops_a_ceiling_taken_from_the_primarys_price() -> None:
    ceiling = {"max_price_prompt": 0.4, "max_price_completion": 1.6}
    derived = PIN.model_copy(update={**ceiling, "derived_ceiling": True})  # what PinStore routes chat with
    made, built = guard(pin=derived)
    assert made.recover("an empty reply")
    assert built[0].max_price_prompt is None
    routed = made.routed(chat_settings(derived, max_tokens=100))
    assert routed is not None and "max_price" not in routed.get("openrouter_provider", {})
    chosen, _ = guard(pin=PIN.model_copy(update=ceiling))  # an owner's own ceiling still holds
    assert chosen.recover("an empty reply")
    kept = chosen.routed(chat_settings(PIN, max_tokens=100))
    assert kept is not None
    assert kept.get("openrouter_provider", {}).get("max_price") == {"prompt": 0.4, "completion": 1.6}
