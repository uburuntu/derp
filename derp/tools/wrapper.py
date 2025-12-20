"""Credit-aware tool wrapper for Pydantic-AI agents.

Provides a decorator that wraps tools with credit checking, ensuring
proper access control and deduction after successful execution.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import TYPE_CHECKING, ParamSpec, TypeVar

import logfire
from pydantic_ai import RunContext

from derp.credits.service import CreditService, get_placeholder_message
from derp.credits.tools import TOOL_REGISTRY

if TYPE_CHECKING:
    from derp.llm.deps import AgentDeps

P = ParamSpec("P")
T = TypeVar("T")


def credit_aware_tool(tool_name: str) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Decorator that wraps a tool with credit checking.

    The wrapped tool will:
    1. Check if the user has credits or free daily limit
    2. Execute the tool if allowed
    3. Deduct credits (or increment daily usage) on success
    4. Return a placeholder message if rejected

    Args:
        tool_name: Name of the tool in TOOL_REGISTRY.

    Returns:
        Decorator function.

    Usage:
        @credit_aware_tool("image_generate")
        async def generate_image(ctx: RunContext[AgentDeps], prompt: str) -> str:
            # Tool implementation
            ...
    """

    def decorator(func: Callable[P, T]) -> Callable[P, T]:
        @wraps(func)
        async def wrapper(
            ctx: RunContext[AgentDeps], *args: P.args, **kwargs: P.kwargs
        ) -> T | str:
            deps = ctx.deps

            # Check if database models are available
            if not deps.user_model or not deps.chat_model:
                logfire.warn(
                    "tool_missing_context",
                    tool=tool_name,
                    has_user_model=deps.user_model is not None,
                    has_chat_model=deps.chat_model is not None,
                )
                return f"[TOOL_ERROR: Missing user or chat context for {tool_name}]"

            # Get or create credit service
            async with deps.db.session() as session:
                service = CreditService(session)

                # Check access
                result = await service.check_tool_access(
                    deps.user_model,
                    deps.chat_model,
                    tool_name,
                    kwargs.get("model"),
                )

                if not result.allowed:
                    logfire.info(
                        "tool_access_denied",
                        tool=tool_name,
                        reason=result.reject_reason,
                        user_id=deps.user_id,
                        chat_id=deps.chat_id,
                    )
                    return get_placeholder_message(
                        tool_name, result.reject_reason or ""
                    )

                # Log access granted with tool call parameters
                # Serialize kwargs for logging (exclude large binary data)
                loggable_kwargs = {
                    k: (
                        f"<{type(v).__name__}:{len(v)} bytes>"
                        if isinstance(v, bytes)
                        else v
                    )
                    for k, v in kwargs.items()
                }
                logfire.info(
                    "tool_invoked",
                    tool=tool_name,
                    source=result.source,
                    credits_to_deduct=result.credits_to_deduct,
                    user_id=deps.user_id,
                    chat_id=deps.chat_id,
                    args=loggable_kwargs,
                )

                # Execute tool
                try:
                    with logfire.span(
                        f"tool.{tool_name}",
                        tool=tool_name,
                        source=result.source,
                        model=result.model_id,
                    ):
                        output = await func(ctx, *args, **kwargs)

                    # Deduct credits on success
                    # Use message_id as part of idempotency key
                    idempotency_key = (
                        f"{tool_name}:{deps.chat_id}:{deps.message.message_id}"
                    )
                    await service.deduct(
                        result,
                        deps.user_model,
                        deps.chat_model,
                        tool_name,
                        idempotency_key=idempotency_key,
                        metadata={
                            "message_id": deps.message.message_id,
                            "source": result.source,
                        },
                    )

                    return output

                except Exception as e:
                    # Don't deduct on failure
                    logfire.exception("tool_execution_failed", tool=tool_name)
                    return f"[TOOL_ERROR: {tool_name} failed - {e}]"

        return wrapper  # type: ignore[return-value]

    return decorator


def is_premium_tool(tool_name: str) -> bool:
    """Check if a tool is marked as premium."""
    tool = TOOL_REGISTRY.get(tool_name)
    return tool.is_premium if tool else False


def get_tool_cost(tool_name: str) -> int:
    """Get the base credit cost of a tool."""
    tool = TOOL_REGISTRY.get(tool_name)
    return tool.base_credit_cost if tool else 0
