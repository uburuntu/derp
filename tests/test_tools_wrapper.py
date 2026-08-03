"""Tests for the legacy credit-aware tool compatibility wrapper."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from derp.catalog import GoogleModelKey
from derp.credits.service import CreditService
from derp.credits.types import CreditCheckResult
from derp.execution import Feature, plan_execution, require_execution_plan
from derp.tools.wrapper import credit_aware_tool


class TransactionRecorder:
    def __init__(self) -> None:
        self.active = 0
        self.sessions: list[MagicMock] = []

    @asynccontextmanager
    async def transaction(self):
        session = MagicMock(name=f"session_{len(self.sessions)}")
        self.sessions.append(session)
        self.active += 1
        try:
            yield session
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_provider_execution_occurs_between_short_credit_transactions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transactions = TransactionRecorder()
    phases: list[str] = []
    plan = plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE)
    access = CreditCheckResult(
        allowed=True,
        plan=plan,
        source="free",
        credits_to_deduct=0,
        credits_remaining=None,
        free_remaining=0,
    )

    async def check_tool_access(
        _service,
        _user,
        _chat,
        tool_name,
        *,
        arguments=None,
    ):
        assert transactions.active == 1
        assert tool_name == "image_generate"
        assert arguments == {"prompt": "draw a lighthouse"}
        phases.append("access")
        return access

    async def deduct(
        _service,
        result,
        _user,
        _chat,
        tool_name,
        *,
        idempotency_key=None,
        metadata=None,
    ) -> None:
        assert transactions.active == 1
        assert result is access
        assert tool_name == "image_generate"
        assert idempotency_key == "image_generate:200:300:tool-call-1"
        assert metadata == {
            "message_id": 300,
            "source": "free",
            "feature": "image_generate",
            "model": plan.model.provider_model_id,
        }
        phases.append("settlement")

    monkeypatch.setattr(CreditService, "check_tool_access", check_tool_access)
    monkeypatch.setattr(CreditService, "deduct", deduct)

    @credit_aware_tool("image_generate")
    async def provider(_ctx, *, prompt: str) -> str:
        assert transactions.active == 0
        assert require_execution_plan(Feature.IMAGE_GENERATE) is plan
        phases.append("provider")
        return f"rendered:{prompt}"

    deps = SimpleNamespace(
        db=SimpleNamespace(session=transactions.transaction),
        user_model=MagicMock(name="user"),
        chat_model=MagicMock(name="chat"),
        user_id=100,
        chat_id=200,
        message=SimpleNamespace(message_id=300),
    )
    ctx = SimpleNamespace(deps=deps, tool_call_id="tool-call-1")

    result = await provider(ctx, prompt="draw a lighthouse")

    assert result == "rendered:draw a lighthouse"
    assert phases == ["access", "provider", "settlement"]
    assert transactions.active == 0
    assert len(transactions.sessions) == 2
