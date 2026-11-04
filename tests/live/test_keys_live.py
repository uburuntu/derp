"""Real contributed-key plumbing (cents of the test key's credits). Run with ``pytest -m live tests/live/test_keys_live.py``.

Needs ``OPENROUTER_MANAGEMENT_KEY`` and ``OPENAI_TEST_KEY`` (an OpenAI API key with a few dollars on it) in
the environment or ``.env``; ``tools/env_secrets.py`` puts them there. Every key it registers is taken out
again, whatever happens.
"""

import os
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from dotenv import dotenv_values
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError

from derp.config import Settings
from derp.inference.api import OpenRouterAPI, OpenRouterError, OpenRouterManagement, SubKey
from derp.inference.openrouter import build_provider, chat_model, chat_settings, key_model, response_usage
from derp.keys.failures import classify
from derp.keys.register import allowed_models
from derp.keys.routing import key_pin

pytestmark = pytest.mark.live
ENV = {**dotenv_values(".env"), **os.environ}
CHEAP = "openai/gpt-6-luna"


def secret(name: str) -> str:
    value = ENV.get(name) or ""
    if not value:
        pytest.skip(f"{name} is not set")
    return value


@pytest.fixture
async def apis() -> AsyncIterator[tuple[Settings, OpenRouterManagement, OpenRouterAPI]]:
    settings = Settings(openrouter_management_key=secret("OPENROUTER_MANAGEMENT_KEY"))
    management, api = OpenRouterManagement(settings), OpenRouterAPI(settings)
    try:
        yield settings, management, api
    finally:
        await management.close()
        await api.close()


async def registered(settings: Settings, management: OpenRouterManagement, raw: str) -> tuple[SubKey, str]:
    sub = await management.create_key("derp live test", limit_usd=1.0)
    try:
        credential = await management.register(
            "openai",
            raw,
            name="derp live test",
            sub_key_hash=sub.hash,
            models=allowed_models(settings, "openai"),
        )
    except OpenRouterError:
        await management.delete_key(sub.hash)
        raise
    print("registered", credential.label)
    return sub, credential.id


async def test_a_registered_key_pays_for_turns_through_its_sub_key_only(
    apis: tuple[Settings, OpenRouterManagement, OpenRouterAPI],
) -> None:
    settings, management, api = apis
    sub, credential = await registered(settings, management, secret("OPENAI_TEST_KEY"))
    try:
        await api.ping(CHEAP, "openai", api_key=sub.secret)
        pin = key_pin("openai", CHEAP)
        agent = Agent(key_model(pin, sub.secret, settings), model_settings=chat_settings(pin, max_tokens=200))
        result = await agent.run("Reply with exactly one word: pong")
        usage = response_usage(result.response)
        print("key turn", usage, result.response.provider_details)
        assert usage.upstream_usd is not None and usage.upstream_usd > 0  # the key's provider billed it
        assert usage.cost_usd < usage.upstream_usd  # OpenRouter's own part: its fee at most

        # Derp's own key is untouched by the registration (shared capacity stays its own for this model).
        own = Agent(
            chat_model(pin, build_provider(settings)), model_settings=chat_settings(pin, max_tokens=200)
        )
        mine = response_usage((await own.run("Reply with exactly one word: pong")).response)
        print("derp turn", mine)
        assert mine.upstream_usd is None and mine.cost_usd > 0
    finally:
        await management.unregister(credential)
        await management.delete_key(sub.hash)


async def test_a_rejected_key_fails_the_ping_and_classifies_as_dead(
    apis: tuple[Settings, OpenRouterManagement, OpenRouterAPI],
) -> None:
    settings, management, api = apis
    try:
        sub, credential = await registered(settings, management, "sk-proj-" + "0" * 48)
    except OpenRouterError as refused:  # OpenRouter may check the key when it is registered
        print("registration refused", refused.status, refused)
        assert refused.status in (400, 401, 403, 422)
        return
    try:
        with pytest.raises(OpenRouterError) as pinged:
            await api.ping(CHEAP, "openai", api_key=sub.secret)
        print("ping", pinged.value.status, pinged.value)
        assert pinged.value.status in (401, 403)
        pin = key_pin("openai", CHEAP)
        agent = Agent(key_model(pin, sub.secret, settings), model_settings=chat_settings(pin, max_tokens=50))
        with pytest.raises(ModelHTTPError) as failed:
            await agent.run("ping")
        print("turn", failed.value.status_code, failed.value.body)
        assert classify(failed.value) == "dead"
    finally:
        await management.unregister(credential)
        await management.delete_key(sub.hash)


async def test_a_key_draws_with_its_providers_image_model(
    apis: tuple[Settings, OpenRouterManagement, OpenRouterAPI],
) -> None:
    settings, management, api = apis
    sub, credential = await registered(settings, management, secret("OPENAI_TEST_KEY"))
    try:
        model = settings.keys.images["openai"]
        image = await api.generate_image(
            model, "a tiny red dot", only=["openai"], zdr=False, api_key=sub.secret
        )
        print("image", image.model, image.cost_usd, image.upstream_usd, len(image.data))
        assert image.data and image.upstream_usd is not None and image.upstream_usd > Decimal(0)
    finally:
        await management.unregister(credential)
        await management.delete_key(sub.hash)
