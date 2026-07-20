"""Deep thinking tool for Pydantic-AI agents.

This tool uses the catalog's current reasoning model for mathematical
problems, logical puzzles, and tasks that require extended thinking.

Reference: https://ai.google.dev/gemini-api/docs/models
"""

from __future__ import annotations

import logfire
from pydantic_ai import RunContext

from derp.execution import Feature, require_execution_plan
from derp.llm.agents import create_chat_agent
from derp.llm.deps import AgentDeps
from derp.observability import report_exception
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
    """Apply the catalog's advanced reasoning model to a complex problem.

    Use this tool when the user asks you to "think harder", "analyze deeply",
    or for complex mathematical or logical problems.

    Args:
        problem: The problem or question to analyze deeply.
    """
    deps = ctx.deps

    logfire.info(
        "deep_thinking_started",
        problem_length=len(problem),
        chat_id=deps.chat_id,
    )

    try:
        agent = create_chat_agent(require_execution_plan(Feature.DEEP_THINK))

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
        report_exception("deep_thinking_failed", chat_id=deps.chat_id)
        return f"Deep thinking failed: {exc!s}"
