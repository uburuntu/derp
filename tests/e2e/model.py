"""A scripted stand-in for the chat model: queued replies, every request recorded.

Replies are strings, responders returning a ``ModelResponse``, or ``Chunks`` for
streamed text with a hook between chunks (e.g. to press "stop" mid-answer).
Runs stream (they pass an event handler), so both the plain and the streaming
paths serve the same queue.
"""

from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from derp.inference.roles import RolePin

Responder = Callable[[list[ModelMessage], AgentInfo], ModelResponse]


@dataclass(slots=True)
class Chunks:
    parts: list[str]
    between: Callable[[int], Awaitable[None]] | None = None


@dataclass(slots=True)
class ScriptedModel:
    replies: deque[str | Responder | Chunks] = field(default_factory=deque[str | Responder | Chunks])
    requests: list[list[ModelMessage]] = field(default_factory=list[list[ModelMessage]])
    infos: list[AgentInfo] = field(default_factory=list[AgentInfo])
    pins: list[RolePin] = field(default_factory=list[RolePin])
    default: str = "ok"

    def reply(self, *items: str | Responder | Chunks) -> None:
        self.replies.extend(items)

    def factory(self, pin: RolePin) -> Model:
        self.pins.append(pin)
        return FunctionModel(self._respond, stream_function=self._stream, model_name=pin.model)

    def _next(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse | Chunks:
        self.requests.append(list(messages))
        self.infos.append(info)
        item = self.replies.popleft() if self.replies else self.default
        if isinstance(item, str):
            return ModelResponse(parts=[TextPart(item)])
        if isinstance(item, Chunks):
            return item
        return item(messages, info)

    def _respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        item = self._next(messages, info)
        return ModelResponse(parts=[TextPart("".join(item.parts))]) if isinstance(item, Chunks) else item

    async def _stream(
        self, messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        item = self._next(messages, info)
        if isinstance(item, Chunks):
            for index, chunk in enumerate(item.parts):
                yield chunk
                if item.between is not None:
                    await item.between(index)
            return
        calls: DeltaToolCalls = {}
        for part in item.parts:
            if isinstance(part, TextPart):
                yield part.content
            elif isinstance(part, ToolCallPart):
                calls[len(calls)] = DeltaToolCall(
                    name=part.tool_name, json_args=part.args_as_json_str(), tool_call_id=part.tool_call_id
                )
        if calls:
            yield calls
