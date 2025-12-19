"""Deep thinking tool for Pydantic-AI agents.

This tool uses Gemini 3 Pro (gemini-3-pro-preview) for complex reasoning tasks.
It's useful for mathematical problems, logical puzzles, or tasks that
require extended thinking capabilities.

Reference: https://ai.google.dev/gemini-api/docs/models#gemini-3
"""

from __future__ import annotations

import logfire
from pydantic_ai import RunContext

from derp.llm.agents import create_chat_agent
from derp.llm.deps import AgentDeps
from derp.llm.providers import ModelTier
from derp.tools.wrapper import credit_aware_tool

# System prompt for deep thinking mode
THINKING_PROMPT = """You are in deep thinking mode. Take your time to carefully analyze
the problem step by step. Show your reasoning process clearly.

For mathematical problems:
- Show each step of your work
- Verify your answer by checking it

For logical puzzles:
- State your assumptions
- Work through the logic systematically
- Consider edge cases

For complex questions:
- Break down the problem into parts
- Address each part thoroughly
- Synthesize a comprehensive answer"""


@credit_aware_tool("think_deep")
async def think_deep(
    ctx: RunContext[AgentDeps],
    problem: str,
) -> str:
    """Apply deep reasoning to a complex problem using the most powerful model.

    Use this tool when the user asks you to "think harder", "analyze deeply",
    or when faced with a complex mathematical or logical problem that would
    benefit from extended reasoning with Gemini 3 Pro.

    Args:
        ctx: The run context with agent dependencies.
        problem: The problem or question to analyze deeply.

    Returns:
        A detailed analysis of the problem.
    """
    deps = ctx.deps

    logfire.info(
        "deep_thinking_started",
        problem_length=len(problem),
        chat_id=deps.chat_id,
    )

    try:
        # Create a PREMIUM tier agent (Gemini 3 Pro) for deep thinking
        agent = create_chat_agent(ModelTier.PREMIUM)

        # Run with the thinking prompt and problem
        prompt = f"{THINKING_PROMPT}\n\n**Problem:**\n{problem}"

        result = await agent.run(
            prompt,
            deps=deps,
        )

        logfire.info(
            "deep_thinking_completed",
            chat_id=deps.chat_id,
            response_length=len(result.output),
        )

        return result.output

    except Exception as exc:
        logfire.exception("deep_thinking_failed", chat_id=deps.chat_id)
        return f"Deep thinking failed: {exc!s}"
