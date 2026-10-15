"""Real OpenRouter calls (fractions of a cent). Run with ``pytest -m live``; never in CI."""

import pytest
from pydantic_ai import Agent

from derp.config import Settings
from derp.inference.openrouter import build_provider, chat_model, chat_settings, response_usage
from derp.inference.roles import ModelRole

pytestmark = pytest.mark.live


@pytest.mark.parametrize("role", [ModelRole.CHAT, ModelRole.CHAT_SPONSORED])
async def test_chat_role_reports_cost_and_served_model(role: ModelRole) -> None:
    settings = Settings()
    pin = settings.model_pins[role]
    agent = Agent(chat_model(pin, build_provider(settings)), model_settings=chat_settings(pin, max_tokens=40))
    result = await agent.run("Reply with exactly one word: pong")
    usage = response_usage(result.response)
    print(role, usage)
    assert usage.cost_usd > 0
    assert usage.model and usage.model.split("/")[0] == pin.model.split("/")[0]
    assert usage.generation_id
