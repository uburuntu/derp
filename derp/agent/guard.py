"""Keeps a run ending in an answer rather than an error.

Wrap-up: once a run reaches its soft caps — the tool calls it may make, its
model requests, three quarters of its wall time, or a money hold that one more
request like the last would reach — every following request sets
``tool_choice='none'`` with a short note to answer now. Tool schemas stay in
the request, so the cached prompt prefix still matches; a tool called anyway
is not run (the model is told to answer). The hard limits stay as a backstop.

Recovery, once per run: a reply with nothing to act on (empty, only thinking,
or cut off by the token limit before any text or complete tool call) or a
request the provider refused to start (rate limit, server error, lost
connection) is retried on the pin's fallback order led by its first fallback,
which then serves the rest of the run; after a token-limit cut the retry gets
twice the output tokens. Retrying continues the run: tools that already ran are
in its history and are not repeated. Failures after streaming began are not
retried (part of the answer may already be on screen).
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any, cast

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, ModelRetry
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestContext, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition

from derp.agent.deps import RunDeps
from derp.inference.roles import RolePin

log = logging.getLogger("derp.agent")

WRAP_UP_AT = 0.75  # share of the wall-time cap after which no new tool round starts
WRAP_UP_NOTE = "No more tools this turn. Answer the current message now with what you have."
EMPTY_NOTE = "Your last reply came back empty. Answer the current message now."
SEARCH_NOTE = "Web search is out of budget for this answer; answer without it (say so if it matters)."
BACKSTOP = 4  # requests and tool calls past the soft caps before the hard limits stop the run
RETRYABLE_STATUS = frozenset({408, 429})


def unanswered(response: ModelResponse) -> bool:
    """Nothing to act on: no text and no complete tool call (empty, only thinking, cut off while thinking)."""
    if any(isinstance(p, TextPart) and p.content.strip() for p in response.parts):
        return False
    calls = [p for p in response.parts if isinstance(p, ToolCallPart)]
    if response.finish_reason == "length":
        return not any(_complete(call) for call in calls)
    return not calls


def _complete(call: ToolCallPart) -> bool:
    try:
        call.args_as_dict(raise_if_invalid=True)
    except ValueError:
        return False
    return True


def retryable(error: Exception) -> bool:
    if isinstance(error, ModelHTTPError):
        return error.status_code in RETRYABLE_STATUS or error.status_code >= 500
    return isinstance(error, ModelAPIError)  # the connection failed before any status


@dataclass
class Guard(AbstractCapability[RunDeps]):
    """One run's soft caps and its single recovery; build a fresh one per run and run ``model(...)``."""

    run_id: int
    pin: RolePin
    model_factory: Callable[[RolePin], Model]
    tool_calls: int  # soft cap on tool calls
    requests: int  # soft cap on model requests
    deadline: float  # event-loop time after which the run wraps up
    near_budget: Callable[[list[ModelMessage]], bool]  # one more request would reach the money hold
    wrapping: bool = False
    recovered: bool = False
    longer: bool = False  # the recovery follows a token-limit cut
    fallback: Model | None = field(default=None, repr=False)

    def model(self, primary: Model) -> Model:
        return _Recovering(primary, self)

    def check(self, ctx: RunContext[RunDeps]) -> bool:
        if not self.wrapping and (
            ctx.usage.tool_calls >= self.tool_calls
            or ctx.usage.requests >= self.requests
            or asyncio.get_running_loop().time() >= self.deadline
            or self.near_budget(ctx.messages)
        ):
            self.wrapping = True
            log.info("run %s wraps up", self.run_id)
        return self.wrapping

    def recover(self, why: str) -> bool:
        """Spend the run's one retry; False when it is already spent."""
        if self.recovered:
            return False
        self.recovered = True
        rotated = self.pin.rotated()
        self.fallback = self.model_factory(rotated) if rotated != self.pin else None
        log.info("run %s retries after %s", self.run_id, why)
        return True

    def routed(self, settings: ModelSettings | None) -> ModelSettings | None:
        """Settings for the model that serves the request after a recovery."""
        if not self.recovered:
            return settings
        updated: dict[str, Any] = dict(settings or {})
        if self.fallback is not None:
            updated["openrouter_models"] = list(self.pin.rotated().fallbacks)
        if self.longer and isinstance(tokens := updated.get("max_tokens"), int):
            updated["max_tokens"] = tokens * 2
        return cast(ModelSettings, updated)

    async def prepare_tools(
        self, ctx: RunContext[RunDeps], tool_defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        self.check(ctx)  # decided before the request is built, so the whole step agrees
        return tool_defs

    async def before_model_request(
        self, ctx: RunContext[RunDeps], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        notes: list[str] = []
        settings: dict[str, Any] = dict(request_context.model_settings or {})
        if self.check(ctx):
            settings["tool_choice"] = "none"
            notes.append(WRAP_UP_NOTE)
        elif ctx.deps.search_withheld:  # web search was left out of this request (see tools/web.py)
            notes.append(SEARCH_NOTE)
        if not notes:
            return request_context
        return replace(  # notes are request-only: history keeps what actually happened
            request_context,
            messages=[*request_context.messages, ModelRequest(parts=[UserPromptPart(n) for n in notes])],
            model_settings=cast(ModelSettings, settings),
        )

    async def after_model_request(
        self, ctx: RunContext[RunDeps], *, request_context: ModelRequestContext, response: ModelResponse
    ) -> ModelResponse:
        if not unanswered(response) or not self.recover("an empty reply"):
            return response
        self.longer = response.finish_reason == "length"
        # A tool call cut off mid-arguments must not be repaired into a call the model never finished.
        response.parts = [p for p in response.parts if not isinstance(p, ToolCallPart)]
        raise ModelRetry(EMPTY_NOTE)

    async def before_tool_execute(
        self, ctx: RunContext[RunDeps], *, call: ToolCallPart, tool_def: ToolDefinition, args: dict[str, Any]
    ) -> dict[str, Any]:
        if self.wrapping:  # the provider ignored tool_choice
            raise ModelRetry(WRAP_UP_NOTE)
        return args


@dataclass(init=False)
class _Recovering(WrapperModel):
    """The run's model: a request the provider refuses to start is retried once on the fallback order,
    which serves every request after a recovery."""

    guard: Guard

    def __init__(self, wrapped: Model, guard: Guard) -> None:
        super().__init__(wrapped)
        self.guard = guard

    def _target(self) -> Model:
        return self.guard.fallback or self.wrapped

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        try:
            return await self._target().request(
                messages, self.guard.routed(model_settings), model_request_parameters
            )
        except Exception as error:
            if not (retryable(error) and self.guard.recover(type(error).__name__)):
                raise
        return await self._target().request(
            messages, self.guard.routed(model_settings), model_request_parameters
        )

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncGenerator[StreamedResponse]:
        async with AsyncExitStack() as stack:
            try:  # only opening the stream is retried; once it streams, errors propagate
                stream = await stack.enter_async_context(
                    self._target().request_stream(
                        messages, self.guard.routed(model_settings), model_request_parameters, run_context
                    )
                )
            except Exception as error:
                if not (retryable(error) and self.guard.recover(type(error).__name__)):
                    raise
                stream = await stack.enter_async_context(
                    self._target().request_stream(
                        messages, self.guard.routed(model_settings), model_request_parameters, run_context
                    )
                )
            yield stream
