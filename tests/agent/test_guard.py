import asyncio
from types import SimpleNamespace
from typing import cast

from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ThinkingPart, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.models.openrouter import OpenRouterModelSettings
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from derp.agent.deps import RunDeps
from derp.agent.guard import Guard, retryable, unanswered
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


def guard(*, deadline: float = 1e12, near: bool = False) -> tuple[Guard, list[RolePin]]:
    built: list[RolePin] = []

    def factory(pin: RolePin) -> Model:
        built.append(pin)
        return TestModel()

    def near_budget(_messages: list[ModelMessage]) -> bool:
        return near

    made = Guard(
        run_id=1,
        pin=PIN,
        model_factory=factory,
        tool_calls=3,
        requests=4,
        deadline=deadline,
        near_budget=near_budget,
    )
    return made, built


def context(*, requests: int = 0, tool_calls: int = 0) -> RunContext[RunDeps]:
    usage = RunUsage(requests=requests, tool_calls=tool_calls)
    return cast(RunContext[RunDeps], SimpleNamespace(usage=usage, messages=[]))


async def test_the_run_wraps_up_at_any_soft_cap_and_stays_wrapped() -> None:
    fresh, _ = guard()
    assert not fresh.check(context(requests=3, tool_calls=2))
    assert fresh.check(context(tool_calls=3))
    assert fresh.check(context())  # sticky for the rest of the run
    assert guard()[0].check(context(requests=4))
    assert guard(near=True)[0].check(context())
    late = asyncio.get_running_loop().time() - 1
    assert guard(deadline=late)[0].check(context())


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
    }
    assert not made.recover("a second failure")
    assert len(built) == 1
