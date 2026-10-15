"""A scripted stand-in for the chat model: queued replies, every request recorded."""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel

from derp.inference.roles import RolePin

Responder = Callable[[list[ModelMessage], AgentInfo], ModelResponse]


@dataclass(slots=True)
class ScriptedModel:
    replies: deque[str | Responder] = field(default_factory=deque[str | Responder])
    requests: list[list[ModelMessage]] = field(default_factory=list[list[ModelMessage]])
    infos: list[AgentInfo] = field(default_factory=list[AgentInfo])
    pins: list[RolePin] = field(default_factory=list[RolePin])
    default: str = "ok"

    def reply(self, *items: str | Responder) -> None:
        self.replies.extend(items)

    def factory(self, pin: RolePin) -> Model:
        self.pins.append(pin)
        return FunctionModel(self._respond, model_name=pin.model)

    def _respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.requests.append(list(messages))
        self.infos.append(info)
        item = self.replies.popleft() if self.replies else self.default
        if isinstance(item, str):
            return ModelResponse(parts=[TextPart(item)])
        return item(messages, info)
