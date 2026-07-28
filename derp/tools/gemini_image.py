"""Pydantic-AI adapters for durable, approval-gated image operations."""

from __future__ import annotations

from pydantic_ai import RunContext, ToolCallPart

from derp.approvals.image_tools import (
    EDIT_IMAGE_TOOL,
    GENERATE_IMAGE_TOOL,
    DeferredImageCall,
)
from derp.catalog import GoogleModelKey
from derp.config import settings
from derp.execution import Feature, plan_execution
from derp.features import (
    ImageAwaitingFunding,
    ImageDelivered,
    ImageDeliveryUncertain,
    ImageInProgress,
    ImageNotCharged,
    ImageRefunded,
)
from derp.llm.deps import AgentDeps

_SENT_DIRECTLY = (
    "[Image delivered directly to chat. Do not output anything else unless the "
    "user asked a follow-up question.]"
)


async def generate_image(
    ctx: RunContext[AgentDeps],
    prompt: str,
    *,
    style: str | None = None,
) -> str:
    """Generate an image after the user approves its exact quote.

    Use this tool when the user asks you to create, generate, draw, or make an
    image. The approved image is delivered directly to the original chat.

    Args:
        prompt: A detailed description of the image to generate.
        style: Optional visual style hint.
    """
    return await _run_approved_image_tool(
        ctx,
        ToolCallPart(
            GENERATE_IMAGE_TOOL,
            {"prompt": prompt, "style": style},
            _required_tool_call_id(ctx),
        ),
    )


async def edit_image(
    ctx: RunContext[AgentDeps],
    edit_prompt: str,
) -> str:
    """Edit the image attached to or replied to by the user's request.

    Use this tool only when the conversation includes an image reference. The
    exact edit is executed after approval and delivered to the original chat.

    Args:
        edit_prompt: Description of the requested image changes.
    """
    return await _run_approved_image_tool(
        ctx,
        ToolCallPart(
            EDIT_IMAGE_TOOL,
            {"edit_prompt": edit_prompt},
            _required_tool_call_id(ctx),
        ),
    )


async def _run_approved_image_tool(
    ctx: RunContext[AgentDeps],
    tool_call: ToolCallPart,
) -> str:
    if not ctx.tool_call_approved:
        raise RuntimeError("image tool execution requires a server-approved call")
    coordinator = ctx.deps.image_operation_coordinator
    context = ctx.deps.image_tool_context
    if coordinator is None or context is None:
        raise RuntimeError("image tool execution context is unavailable")
    finishing_quote_input = context.finishing_quote_input
    if finishing_quote_input is None:
        raise RuntimeError("image tool finishing quote is unavailable")

    call = DeferredImageCall.parse(tool_call, source=context.source)
    outcome = ctx.deps.image_operation_outcome
    if outcome is None:
        outcome = await coordinator.run(
            call.invocation(context),
            plan_execution(
                call.feature,
                GoogleModelKey.IMAGE,
                provider=settings.inference_provider(call.feature),
            ),
            call.request,
            allow_personal_once=context.allow_personal_once,
            finishing_plan=plan_execution(
                Feature.CHAT,
                finishing_quote_input.model_key,
                provider=ctx.deps.model.provider,
            ),
            finishing_quote_input=finishing_quote_input,
        )
    ctx.deps.image_operation_outcome = outcome
    if isinstance(outcome, ImageDelivered):
        return _SENT_DIRECTLY
    if isinstance(outcome, ImageAwaitingFunding):
        return (
            f"Image not generated: {outcome.quote.credits} credits are required. "
            "The user was not charged."
        )
    if isinstance(outcome, ImageNotCharged):
        return "Image generation did not complete. The user was not charged."
    if isinstance(outcome, ImageRefunded):
        return "Image delivery failed. The charged credits were refunded."
    if isinstance(outcome, ImageDeliveryUncertain):
        return (
            "Image delivery is uncertain and may already have succeeded. "
            "Do not request another image automatically."
        )
    if isinstance(outcome, ImageInProgress):
        return "The approved image operation is still in progress."
    raise TypeError(f"unsupported image outcome: {type(outcome).__name__}")


def _required_tool_call_id(ctx: RunContext[AgentDeps]) -> str:
    if ctx.tool_call_id is None or not ctx.tool_call_id.strip():
        raise RuntimeError("approved image tool call has no stable ID")
    return ctx.tool_call_id


__all__ = ["edit_image", "generate_image"]
