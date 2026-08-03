"""TTS adapter for the durable paid-media approval workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final

from pydantic_ai import ModelMessage, ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.messages import UserPromptPart

from derp.approvals.paid_media import PaidMediaApprovalKind
from derp.catalog import GoogleModelKey, InferenceProvider
from derp.execution import (
    ExecutionPlan,
    Failed,
    Feature,
    Outcome,
    Rejected,
    Succeeded,
    plan_execution,
)
from derp.features.paid_media_operation import PaidMediaResult
from derp.features.tts import TtsFeatureService, TtsRequest
from derp.history.core import DEFAULT_TOKEN_ESTIMATOR
from derp.operations import TtsQuoteInput

TTS_COMMAND_TOOL: Final = "command_tts"
TTS_COMMAND_CALL_ID: Final = "command-tts"
TTS_PLAN: Final = plan_execution(
    Feature.TTS,
    GoogleModelKey.TTS,
    provider=InferenceProvider.GOOGLE,
)


class DeferredTtsCallError(ValueError):
    """Persisted TTS arguments do not match the command schema."""


@dataclass(frozen=True, slots=True)
class TtsPaidMediaAdapter:
    """Map validated TTS calls onto the generic paid-media operation core."""

    service: TtsFeatureService
    plan: ExecutionPlan = TTS_PLAN

    kind: ClassVar[PaidMediaApprovalKind] = PaidMediaApprovalKind.TTS
    tool_name: ClassVar[str] = TTS_COMMAND_TOOL

    def parse_tool_call(self, tool_call: ToolCallPart) -> TtsRequest:
        """Reconstruct TTS input only from framework-validated persisted args."""
        if not isinstance(tool_call, ToolCallPart):
            raise DeferredTtsCallError("TTS call must be a ToolCallPart")
        if tool_call.tool_name != self.tool_name:
            raise DeferredTtsCallError("persisted tool is not a TTS command")
        try:
            arguments = tool_call.args_as_dict(raise_if_invalid=True)
        except Exception:
            raise DeferredTtsCallError("TTS arguments are invalid") from None
        if set(arguments) != {"text", "max_output_seconds"}:
            raise DeferredTtsCallError("TTS argument shape is invalid")
        try:
            return TtsRequest(
                text=arguments["text"],
                max_output_seconds=arguments["max_output_seconds"],
            )
        except TypeError, ValueError:
            raise DeferredTtsCallError("TTS arguments are invalid") from None

    @staticmethod
    def quote_input(request: TtsRequest) -> TtsQuoteInput:
        """Price the declared provider duration and estimated text input."""
        if not isinstance(request, TtsRequest):
            raise TypeError("request must be a TtsRequest")
        return TtsQuoteInput(
            input_tokens=DEFAULT_TOKEN_ESTIMATOR.estimate_text(request.text),
            output_seconds=request.max_output_seconds,
        )

    async def execute(
        self,
        plan: ExecutionPlan,
        request: TtsRequest,
    ) -> Outcome[PaidMediaResult]:
        """Wrap one delivery-ready voice in the shared immutable result type."""
        outcome = await self.service.synthesize(plan, request)
        if isinstance(outcome, Succeeded):
            return Succeeded(PaidMediaResult((outcome.value,)))
        if isinstance(outcome, (Rejected, Failed)):
            return outcome
        raise TypeError("TTS feature service returned an unsupported outcome")


def tts_command_tool_call(request: TtsRequest) -> ToolCallPart:
    """Build the one native validated call persisted for command approval."""
    if not isinstance(request, TtsRequest):
        raise TypeError("request must be a TtsRequest")
    return ToolCallPart(
        TTS_COMMAND_TOOL,
        {
            "text": request.text,
            "max_output_seconds": request.max_output_seconds,
        },
        TTS_COMMAND_CALL_ID,
    )


def tts_command_history(
    request: TtsRequest,
    tool_call: ToolCallPart,
    plan: ExecutionPlan = TTS_PLAN,
) -> tuple[ModelMessage, ...]:
    """Build minimal native history accepted by durable deferred serialization."""
    if tool_call.tool_name != TTS_COMMAND_TOOL:
        raise ValueError("tool_call must be the TTS command call")
    return (
        ModelRequest(parts=[UserPromptPart(request.text)]),
        ModelResponse(
            parts=[tool_call],
            model_name=plan.model.provider_model_id,
        ),
    )


__all__ = [
    "TTS_COMMAND_CALL_ID",
    "TTS_COMMAND_TOOL",
    "TTS_PLAN",
    "DeferredTtsCallError",
    "TtsPaidMediaAdapter",
    "tts_command_history",
    "tts_command_tool_call",
]
