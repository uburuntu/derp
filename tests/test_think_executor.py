"""Provider adapter tests for deep reasoning."""

from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from derp.catalog import GoogleModelKey
from derp.execution import Feature, Rejected, RejectionReason, Succeeded, plan_execution
from derp.features.think import PreparedThinkRequest
from derp.features.types import TextOutput
from derp.llm.think_executor import THINK_INSTRUCTIONS, PydanticAIThinkExecutor


@pytest.mark.asyncio
async def test_executor_uses_exact_plan_limit_and_problem() -> None:
    plan = plan_execution(Feature.DEEP_THINK, GoogleModelKey.CHAT_REASONING)
    agent = SimpleNamespace(
        run=AsyncMock(return_value=SimpleNamespace(output="  42  "))
    )
    seen: list[tuple[object, int]] = []

    def factory(received_plan, max_output_tokens):
        seen.append((received_plan, max_output_tokens))
        return agent

    result = await PydanticAIThinkExecutor(factory).reason(
        plan,
        PreparedThinkRequest("What is the answer?", 4096),
    )

    assert result == Succeeded(TextOutput("42"))
    assert seen == [(plan, 4096)]
    agent.run.assert_awaited_once_with("What is the answer?")


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ["", "   ", object()])
async def test_executor_maps_unusable_output_without_leaking_provider_details(
    output,
) -> None:
    agent = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(output=output)))
    executor = PydanticAIThinkExecutor(lambda plan, limit: agent)

    result = await executor.reason(
        plan_execution(Feature.DEEP_THINK, GoogleModelKey.CHAT_REASONING),
        PreparedThinkRequest("Question", 1024),
    )

    assert result == Rejected(RejectionReason.UNUSABLE_OUTPUT)


def test_instructions_require_checkable_answers_without_hidden_reasoning() -> None:
    lowered = THINK_INSTRUCTIONS.lower()

    assert "checkable" in lowered
    assert "do not reveal hidden chain-of-thought" in lowered
    assert "invent sources" in lowered


def test_think_boundaries_do_not_import_product_side_effect_layers() -> None:
    from derp.features import think as feature_module
    from derp.llm import think_executor as executor_module

    forbidden = (
        "aiogram",
        "logfire",
        "derp.common.sender",
        "derp.config",
        "derp.credits",
        "derp.db",
        "derp.models",
    )
    for module in (feature_module, executor_module):
        tree = ast.parse(inspect.getsource(module))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert not any(
            name == prefix or name.startswith(f"{prefix}.")
            for name in imported
            for prefix in forbidden
        )
