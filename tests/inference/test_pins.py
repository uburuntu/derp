import json
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.providers.openrouter import OpenRouterProvider

from derp.config import Settings
from derp.db.engine import Database
from derp.inference.models import ModelPin
from derp.inference.openrouter import chat_model, chat_settings
from derp.inference.pins import PinStore, apply_pin, rollback_pin
from derp.inference.roles import ModelRole, RolePin

pytestmark = pytest.mark.db


async def snapshot(database: Database, role: ModelRole, prompt: str, completion: str | None) -> None:
    async with database.transaction() as session:
        row = await session.get(ModelPin, role.value)
        assert row is not None
        row.price_prompt = Decimal(prompt)
        row.price_completion = Decimal(completion) if completion is not None else None


async def sent_body(pin: RolePin) -> dict[str, Any]:
    """The JSON body a chat request on this pin sends to OpenRouter."""
    bodies: list[dict[str, Any]] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "gen-1",
                "object": "chat.completion",
                "created": 0,
                "model": pin.model,
                "provider": "Test",
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4, "cost": 0.00001},
            },
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        model = chat_model(pin, OpenRouterProvider(api_key="test-key", http_client=client))
        await model.request(
            [ModelRequest(parts=[UserPromptPart("hello")])],
            chat_settings(pin, max_tokens=100),
            ModelRequestParameters(),
        )
    return bodies[0]


async def test_chat_roles_route_under_twice_their_price_snapshot(
    database: Database, settings: Settings
) -> None:
    store = PinStore(database, settings)
    await store.seed()
    await snapshot(database, ModelRole.CHAT, "0.2", "0.8")  # USD per 1M tokens
    await snapshot(database, ModelRole.CHAT_SPONSORED, "0.1", None)
    await snapshot(database, ModelRole.CHAT_FREE, "0.1", "0.1")
    await store.refresh()

    chat = await store.get(ModelRole.CHAT)
    assert (chat.max_price_prompt, chat.max_price_completion) == (0.4, 1.6)
    retry = chat.rotated()  # led by a fallback the primary's price says nothing about: no ceiling
    assert (retry.max_price_prompt, retry.max_price_completion) == (None, None)
    assert "max_price" not in (await sent_body(retry))["provider"]
    sponsored = await store.get(ModelRole.CHAT_SPONSORED)
    assert (sponsored.max_price_prompt, sponsored.max_price_completion) == (0.2, None)
    assert (await store.get(ModelRole.CHAT_FREE)).max_price_prompt is None

    body = await sent_body(chat)
    assert body["provider"]["max_price"] == {"prompt": 0.4, "completion": 1.6}
    assert body["provider"]["zdr"] is True
    assert (await sent_body(sponsored))["provider"]["max_price"] == {"prompt": 0.2}

    # The stored pins stay as chosen, so the scout never writes the derived ceiling back.
    stored = await store.all()
    assert stored[ModelRole.CHAT].max_price_prompt is None
    assert stored[ModelRole.CHAT] == settings.model_pins[ModelRole.CHAT]


async def test_no_snapshot_no_ceiling_and_a_pin_of_its_own_wins(
    database: Database, settings: Settings
) -> None:
    store = PinStore(database, settings)
    await store.seed()
    await store.refresh()
    assert "max_price" not in (await sent_body(await store.get(ModelRole.CHAT)))["provider"]

    own = RolePin(model="a/b", fallbacks=("c/d",), max_price_prompt=5.0)
    async with database.transaction() as session:
        await apply_pin(session, ModelRole.CHAT, own, source="owner")
    await snapshot(database, ModelRole.CHAT, "0.2", "0.8")
    await store.refresh()
    chat = await store.get(ModelRole.CHAT)
    assert (chat.max_price_prompt, chat.max_price_completion) == (5.0, None)
    assert chat.rotated().max_price_prompt == 5.0  # a ceiling someone chose covers the retry too


async def test_a_new_model_drops_the_old_models_snapshot(database: Database, settings: Settings) -> None:
    store = PinStore(database, settings)
    await store.seed()
    await snapshot(database, ModelRole.CHAT, "0.2", "0.8")
    default = settings.model_pins[ModelRole.CHAT]
    async with database.transaction() as session:  # same model, other settings: the snapshot still holds
        await apply_pin(session, ModelRole.CHAT, default.model_copy(update={"fast": False}), source="owner")
    async with database.session() as session:
        row = await session.get(ModelPin, ModelRole.CHAT.value)
        assert row is not None and row.price_prompt == Decimal("0.2")

    async with database.transaction() as session:
        await apply_pin(session, ModelRole.CHAT, RolePin(model="other/model"), source="owner")
    await store.refresh()
    assert (await store.get(ModelRole.CHAT)).max_price_prompt is None  # never cap a model by another's price

    await snapshot(database, ModelRole.CHAT, "3", "9")
    async with database.transaction() as session:
        assert await rollback_pin(session, ModelRole.CHAT)
    async with database.session() as session:
        row = await session.get(ModelPin, ModelRole.CHAT.value)
        assert row is not None and row.price_prompt is None and row.pin["model"] == default.model
