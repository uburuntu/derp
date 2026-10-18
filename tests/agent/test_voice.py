"""Voice lines: priced from the speech pin's snapshot, and a call that may have been made is never repeated."""

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import select

from derp.agent.deps import RunDeps
from derp.agent.tools.voice import Rates, _perform, rates
from derp.config import Settings
from derp.db.engine import Database
from derp.inference.api import OpenRouterError, Speech
from derp.inference.models import ModelPin, ProviderCall
from derp.inference.pins import PinStore
from derp.inference.roles import ModelRole, RolePin

TURBO, FULL = "elevenlabs/eleven-v4-turbo", "elevenlabs/eleven-v4"
PIN = RolePin(model=TURBO, fallbacks=(FULL,), reasoning=None, vision=False)
PRICES = Rates(PIN, {TURBO: 20.0, FULL: 80.0})


class Speaker:
    """The speech API: each model fails with its status (0: timed out), hangs, or speaks."""

    def __init__(self, **outcomes: int | None) -> None:
        self.outcomes = outcomes
        self.tried: list[str] = []
        self.started = asyncio.Event()

    async def speak(self, model: str, text: str, voice: str, *, cost_usd: Decimal, zdr: bool) -> Speech:
        self.tried.append(model)
        self.started.set()
        status = self.outcomes.get(model.rsplit("-", 1)[-1])
        if status == -1:
            await asyncio.Event().wait()  # never answers
        if status is not None:
            raise OpenRouterError(status, "nope")
        return Speech(b"ID3", model, cost_usd, len(text), None)


def deps(database: Database, settings: Settings, api: object = None) -> RunDeps:
    return cast(
        RunDeps,
        SimpleNamespace(
            db=database, settings=settings, pins=PinStore(database, settings), api=api,
            chat=SimpleNamespace(id=-1), user_id=1,
        ),
    )  # fmt: skip


async def creator_calls(database: Database) -> list[tuple[Any, ...]]:
    async with database.session() as session:
        rows = await session.scalars(select(ProviderCall).where(ProviderCall.kind == "speech"))
        return [(c.model, c.funding, c.run_id, c.units, c.cost_usd) for c in rows]


@pytest.mark.db
async def test_lines_are_priced_at_the_speech_pins_snapshot_else_list_prices(
    database: Database, settings: Settings
) -> None:
    listed = await rates(deps(database, settings))
    assert listed.usd_per_m == {TURBO: 40.0, FULL: 80.0}  # no snapshot yet: the fallback list prices
    async with database.transaction() as session:
        pin = settings.model_pins[ModelRole.SPEECH].model_dump(mode="json")
        session.add(ModelPin(role="speech", pin=pin, price_prompt=Decimal(20), price_completion=Decimal(0)))
    snapshot = await rates(deps(database, settings))
    assert snapshot.usd_per_m == {TURBO: 20.0, FULL: 80.0}  # OpenRouter's (sale) price for the primary
    assert snapshot.usd(TURBO, 400) == Decimal("0.008")
    assert snapshot.most(400) == Decimal("0.032")  # a fallback may voice it: checked at the dearest


@pytest.mark.db
async def test_a_refused_model_falls_back_but_a_timed_out_one_is_ledgered_and_never_retried(
    database: Database, settings: Settings
) -> None:
    refused = Speaker(turbo=503)
    speech = await _perform(deps(database, settings, refused), PRICES, "[laughs] ha", "callum")
    assert refused.tried == [TURBO, FULL] and speech.model == FULL
    assert await creator_calls(database) == []  # a refusal is never billed

    timed_out = Speaker(turbo=0)
    with pytest.raises(OpenRouterError):
        await _perform(deps(database, settings, timed_out), PRICES, "[laughs] ha", "callum")
    assert timed_out.tried == [TURBO]  # no second paid call
    # It may still be billed: the estimate, on Derp, without a run.
    assert await creator_calls(database) == [(TURBO, "creator", None, 11, Decimal("0.00022"))]


@pytest.mark.db
async def test_a_line_stopped_mid_call_is_ledgered_at_its_estimate(
    database: Database, settings: Settings
) -> None:
    hanging = Speaker(turbo=-1)
    call = asyncio.create_task(_perform(deps(database, settings, hanging), PRICES, "[sighs] ну", "lily"))
    await hanging.started.wait()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert await creator_calls(database) == [(TURBO, "creator", None, 10, Decimal("0.0002"))]
