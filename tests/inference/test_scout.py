from decimal import Decimal
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from derp.config import Settings
from derp.db.engine import Database
from derp.inference.models import ModelPin, ProviderCall, ScoutProposal
from derp.inference.pins import PinStore, rollback_pin
from derp.inference.roles import DEFAULT_PINS, ModelRole, RolePin
from derp.inference.scout import build_catalog, passes_smoke, plan, run_scout

ALLOW = ("openai/", "deepseek/", "acme/")
WINDOWS = {ModelRole.CHAT: 16_000, ModelRole.CHAT_SPONSORED: 8_000}


def model(id_: str, iq: float | None, prompt: str, completion: str, *, image: bool = True) -> dict[str, Any]:
    return {
        "id": id_,
        "pricing": {"prompt": prompt, "completion": completion},
        "supported_parameters": ["tools"],
        "architecture": {"input_modalities": ["text", "image"] if image else ["text"]},
        "benchmarks": {"artificial_analysis": {"intelligence_index": iq}},
    }


def route(id_: str, prompt: str, completion: str, uptime: float | None = 100) -> dict[str, Any]:
    return {
        "model_id": id_,
        "tag": "host",
        "pricing": {"prompt": prompt, "completion": completion},
        "uptime_last_1d": uptime,
        "supported_parameters": ["tools"],
    }


def test_dead_primary_promotes_live_fallback() -> None:
    pins = {ModelRole.CHAT: RolePin(model="openai/a", fallbacks=("openai/b",))}
    catalog = build_catalog(
        [model("openai/a", 30, "0.0000001", "0.0000005"), model("openai/b", 30, "0.0000001", "0.0000005")],
        [route("openai/a", "0.0000001", "0.0000005", uptime=50), route("openai/b", "0.0000001", "0.0000005")],
        [],
    )
    changes = plan(pins, catalog, allowlist=ALLOW, windows=WINDOWS)
    promoted = next(c for c in changes if c.reason != "price snapshot")
    assert promoted.safe and promoted.pin.model == "openai/b" and promoted.pin.fallbacks == ("openai/a",)
    price = next(c for c in changes if c.reason == "price snapshot")
    assert price.price == (Decimal("0.1"), Decimal("0.5"))


def test_challenger_needs_more_intelligence_at_no_higher_turn_cost() -> None:
    pins = {ModelRole.CHAT: RolePin(model="openai/a")}
    models = [
        model("openai/a", 38, "0.0000001", "0.0000005"),
        model("acme/smart", 45, "0.00000009", "0.0000006"),
        model("acme/pricey", 60, "0.000002", "0.00001"),
        model("acme/blind", 70, "0.00000001", "0.0000001", image=False),
        model("other/smart", 70, "0.00000001", "0.0000001"),
    ]
    routes = [route(m["id"], m["pricing"]["prompt"], m["pricing"]["completion"]) for m in models]
    changes = plan(pins, build_catalog(models, routes, []), allowlist=ALLOW, windows=WINDOWS)
    proposal = next(c for c in changes if not c.safe)
    assert proposal.pin.model == "acme/smart"
    assert proposal.pin.fallbacks == ("openai/a",)


def test_turn_prices_count_the_prompt_cache() -> None:
    """A smarter model with cheaper output but a dearer cache read only looks cheaper when nothing is cached."""
    pins = {ModelRole.CHAT: RolePin(model="deepseek/snapshot")}
    models = [
        model("deepseek/snapshot", 39.5, "0.00000015", "0.0000006"),
        model("acme/glm", 41.8, "0.00000015", "0.0000005"),
    ]
    routes = [
        {**route("deepseek/snapshot", "0.00000015", "0.0000006"), "pricing": {
            "prompt": "0.00000015", "completion": "0.0000006", "input_cache_read": "0.000000003"}},
        {**route("acme/glm", "0.00000015", "0.0000005"), "pricing": {
            "prompt": "0.00000015", "completion": "0.0000005", "input_cache_read": "0.00000003"}},
    ]  # fmt: skip
    catalog = build_catalog(models, routes, [])
    uncached = [c for c in plan(pins, catalog, allowlist=ALLOW, windows=WINDOWS) if not c.safe]
    assert [c.pin.model for c in uncached] == ["acme/glm"]
    assert not [c for c in plan(pins, catalog, allowlist=ALLOW, windows=WINDOWS, cached=0.7) if not c.safe]


def test_free_role_availability_uses_catalog_not_zdr() -> None:
    pins = {ModelRole.CHAT_FREE: RolePin(model="x/free:free", fallbacks=("y/free:free",), zdr=False)}
    catalog = build_catalog([model("y/free:free", None, "0", "0")], [], [])
    (change,) = plan(pins, catalog, allowlist=ALLOW, windows=WINDOWS)
    assert change.pin.model == "y/free:free"


def test_the_decision_pin_lives_on_a_zdr_route_without_tools_and_gets_no_challengers() -> None:
    pins = {ModelRole.DECISION: RolePin(model="typesafe/jev-1.14", reasoning=None, vision=False)}
    jev = route("typesafe/jev-1.14", "0.000000042", "0") | {"supported_parameters": []}
    catalog = build_catalog([model("deepseek/smart", 90, "0.0000001", "0.0000001")], [jev], [])
    assert plan(pins, catalog, allowlist=ALLOW, windows=WINDOWS) == []


def test_the_speech_pin_lives_on_a_zdr_route_without_tools_and_falls_back_when_dead() -> None:
    pin = RolePin(model="elevenlabs/turbo", fallbacks=("elevenlabs/full",), reasoning=None, vision=False)
    turbo = route("elevenlabs/turbo", "0.00002", "0") | {"supported_parameters": []}
    full = route("elevenlabs/full", "0.00004", "0") | {"supported_parameters": []}
    pins = {ModelRole.SPEECH: pin}
    assert plan(pins, build_catalog([], [turbo, full], []), allowlist=ALLOW, windows=WINDOWS) == []
    [change] = plan(pins, build_catalog([], [full], []), allowlist=ALLOW, windows=WINDOWS)
    assert change.safe and change.pin.model == "elevenlabs/full"


def test_the_media_pin_is_priced_from_its_standard_routes_only() -> None:
    """Flex and priority endpoints need an opt-in Derp never sends: their prices aren't what a look costs."""
    pins = {ModelRole.MEDIA: DEFAULT_PINS[ModelRole.MEDIA]}
    lite = DEFAULT_PINS[ModelRole.MEDIA].model
    standard = route(lite, "0.0000003", "0.0000025") | {"supported_parameters": []}
    flex = standard | {"tag": "google-vertex/global/flex", "service_tier": "flex", "pricing": {
        "prompt": "0.00000015", "completion": "0.00000125"}}  # fmt: skip
    (change,) = plan(pins, build_catalog([], [standard, flex], []), allowlist=ALLOW, windows=WINDOWS)
    assert change.reason == "price snapshot" and change.price == (Decimal("0.3"), Decimal("2.5"))
    assert not plan(pins, build_catalog([], [flex], []), allowlist=ALLOW, windows=WINDOWS)  # no live route


def scripted(*, russian: str, call_tool: bool) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        last = str(messages[-1].parts[-1])
        if "tool-return" in last or "ToolReturnPart" in last:
            return ModelResponse(parts=[TextPart("It is noon.")])
        if "Use the tool" in last and call_tool:
            return ModelResponse(parts=[ToolCallPart("current_time", {})])
        if "по-русски" in last:
            return ModelResponse(parts=[TextPart(russian)])
        return ModelResponse(parts=[TextPart("ok")])

    return FunctionModel(respond)


async def test_smoke_check() -> None:
    pin = RolePin(model="acme/smart")
    ok, paid = await passes_smoke(scripted(russian="Небо голубое.", call_tool=True), pin)
    assert ok and len(paid) == 3  # russian answer + tool call + final answer, all ledgered by the caller
    assert not (await passes_smoke(scripted(russian="The sky is blue.", call_tool=True), pin))[0]
    assert not (await passes_smoke(scripted(russian="Небо голубое.", call_tool=False), pin))[0]


def test_missing_prices_are_not_free() -> None:
    priced = model("acme/a", 50, "0.0000001", "0.0000005")
    unpriced = {**model("acme/b", 60, "0", "0"), "pricing": {}}
    catalog = build_catalog([priced, unpriced], [{"model_id": "acme/b", "pricing": {"prompt": "0"}}], [])
    assert set(catalog.models) == {"acme/a"}
    assert catalog.zdr == {}


class FakeAPI:
    def __init__(self, models: list[dict[str, Any]], zdr: list[dict[str, Any]]) -> None:
        self._models, self._zdr = models, zdr

    async def models(self, **_: str) -> list[dict[str, Any]]:
        return self._models

    async def zdr_endpoints(self) -> list[dict[str, Any]]:
        return self._zdr

    async def image_models(self) -> list[dict[str, Any]]:
        return [{"id": "google/gemini-3.1-flash-lite-image"}, {"id": "bytedance-seed/seedream-5-0-flash"}]


@pytest.mark.db
async def test_run_scout_applies_safe_changes_and_files_one_proposal(
    database: Database, settings: Settings
) -> None:
    pins = {
        ModelRole.CHAT: RolePin(model="openai/a", fallbacks=("openai/b",)),
        ModelRole.CHAT_SPONSORED: RolePin(model="deepseek/s", reasoning="none"),
    }
    configured = settings.model_copy(update={"model_pins": pins, "model_family_allowlist": ALLOW})
    store = PinStore(database, configured)
    await store.seed()
    models = [
        model("openai/a", 38, "0.0000001", "0.0000005"),
        model("openai/b", 38, "0.0000001", "0.0000005"),
        model("deepseek/s", 30, "0.00000001", "0.0000007"),
        model("acme/smart", 45, "0.00000009", "0.0000006"),
    ]
    zdr = [route(m["id"], m["pricing"]["prompt"], m["pricing"]["completion"]) for m in models[1:]]
    notes: list[str] = []
    announced: list[int] = []

    async def notify(text: str, proposals: list[int]) -> None:
        notes.append(text)
        announced.extend(proposals)

    factory = lambda _pin: scripted(russian="Небо голубое.", call_tool=True)
    await run_scout(database, FakeAPI(models, zdr), store, configured, factory, notify)  # type: ignore[arg-type]
    await run_scout(database, FakeAPI(models, zdr), store, configured, factory, notify)  # type: ignore[arg-type]

    assert (await store.get(ModelRole.CHAT)).model == "openai/b"
    async with database.session() as session:
        proposals = list(await session.scalars(select(ScoutProposal)))
        assert [p.proposed["model"] for p in proposals] == ["acme/smart"]
        assert announced == [proposals[0].id]  # its ping carries Apply / Ignore; the repeat files nothing
        row = await session.get(ModelPin, "chat")
        assert row is not None and row.price_prompt == Decimal("0.1")
    async with database.transaction() as session:
        assert await rollback_pin(session, ModelRole.CHAT)
    await store.refresh()
    assert (await store.get(ModelRole.CHAT)).model == "openai/a"
    assert notes and "promoted openai/b" in notes[0]
    async with database.session() as session:
        probes = list(await session.scalars(select(ProviderCall).where(ProviderCall.kind == "scout")))
        assert probes and {p.funding for p in probes} == {
            "creator"
        }  # one probe run; the repeat was deduplicated


@pytest.mark.db
async def test_unknown_pin_roles_are_ignored(database: Database, settings: Settings) -> None:
    store = PinStore(database, settings)
    await store.seed()
    async with database.transaction() as session:
        session.add(ModelPin(role="retired_role", pin={"model": "x/y"}, source="seed"))
    assert (await store.get(ModelRole.CHAT)).model == settings.model_pins[ModelRole.CHAT].model


@pytest.mark.db
async def test_seed_rows_follow_new_defaults_but_choices_stay(database: Database, settings: Settings) -> None:
    async with database.transaction() as session:
        session.add(
            ModelPin(
                role=ModelRole.CHAT.value, pin={"model": "old/default"}, source="seed",
                price_prompt=Decimal(1), price_completion=Decimal(2),
            )
        )  # fmt: skip
        session.add(
            ModelPin(role=ModelRole.CHAT_SPONSORED.value, pin={"model": "owner/choice"}, source="owner")
        )
    await PinStore(database, settings).seed()
    async with database.session() as session:
        chat = await session.get(ModelPin, ModelRole.CHAT.value)
        sponsored = await session.get(ModelPin, ModelRole.CHAT_SPONSORED.value)
    assert chat is not None and sponsored is not None
    assert chat.pin == settings.model_pins[ModelRole.CHAT].model_dump(mode="json") and chat.source == "seed"
    assert chat.previous == {"model": "old/default"} and chat.price_prompt is None  # the scout re-prices it
    assert sponsored.pin == {"model": "owner/choice"}
