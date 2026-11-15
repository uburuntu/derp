"""Voice lines: voiced by the first model able to act them, priced from the speech pin's snapshot, and a call that
may have been made is never repeated."""

import asyncio
import io
import wave
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import select

from derp.agent.deps import RunDeps
from derp.agent.tools import voice
from derp.agent.tools.voice import (
    Line,
    Rates,
    _perform,
    affordable,
    can_voice,
    is_direction,
    joined,
    line_for,
    rates,
    voicing,
)
from derp.config import Settings, SpeechModel
from derp.db.engine import Database
from derp.inference.api import OpenRouterError, Speech, as_wav
from derp.inference.models import ModelPin, ProviderCall
from derp.inference.pins import PinStore
from derp.inference.roles import ModelRole, RolePin

GEMINI, TURBO, FULL = "google/gemini-3.8-flash-lite-tts", "elevenlabs/eleven-v4-turbo", "elevenlabs/eleven-v4"
PIN = RolePin(model=TURBO, fallbacks=(FULL,), reasoning=None, vision=False)
PRICES = Rates(PIN, {TURBO: 20.0, FULL: 80.0})
ELEVEN = (TURBO, FULL)


class Speaker:
    """The speech API: each model fails with its status (0: timed out), hangs, or speaks."""

    def __init__(self, **outcomes: int | None) -> None:
        self.outcomes = outcomes
        self.tried: list[str] = []
        self.started = asyncio.Event()

    async def speak(
        self, model: str, text: str, voice: str, *, cost_usd: Decimal, zdr: bool, **_: object
    ) -> Speech:
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
    assert listed.usd_per_m == {GEMINI: 16.0, TURBO: 40.0, FULL: 80.0}  # no snapshot yet: list prices
    async with database.transaction() as session:
        pin = settings.model_pins[ModelRole.SPEECH].model_dump(mode="json")
        session.add(
            ModelPin(role="speech", pin=pin, price_prompt=Decimal("0.5"), price_completion=Decimal(6))
        )
    tokens = await rates(deps(database, settings))
    assert tokens.usd_per_m[GEMINI] == 16.0  # priced per token: its snapshot says nothing per character

    turbo_first = settings.model_copy(update={"model_pins": settings.model_pins | {ModelRole.SPEECH: PIN}})
    async with database.transaction() as session:
        row = await session.get(ModelPin, "speech")
        assert row is not None
        row.pin, row.price_prompt, row.price_completion = PIN.model_dump(mode="json"), Decimal(20), Decimal(0)
    snapshot = await rates(deps(database, turbo_first))
    assert snapshot.usd_per_m == {TURBO: 20.0, FULL: 80.0}  # OpenRouter's (sale) price for the primary
    assert snapshot.usd(TURBO, 400) == Decimal("0.008")
    assert snapshot.most(400) == Decimal("0.032")  # a fallback may voice it: checked at the dearest
    assert snapshot.most(400, (TURBO,)) == Decimal("0.008")  # only the models able to act the line


@pytest.mark.db
async def test_a_refused_model_falls_back_but_a_timed_out_one_is_ledgered_and_never_retried(
    database: Database, settings: Settings
) -> None:
    refused = Speaker(turbo=503)
    speech = await _perform(deps(database, settings, refused), PRICES, "[laughs] ha", "callum", ELEVEN)
    assert refused.tried == [TURBO, FULL] and speech.model == FULL
    assert await creator_calls(database) == []  # a refusal is never billed

    timed_out = Speaker(turbo=0)
    with pytest.raises(OpenRouterError):
        await _perform(deps(database, settings, timed_out), PRICES, "[laughs] ha", "callum", ELEVEN)
    assert timed_out.tried == [TURBO]  # no second paid call
    # It may still be billed: the estimate, on Derp, without a run.
    assert await creator_calls(database) == [(TURBO, "creator", None, 11, Decimal("0.00022"))]


@pytest.mark.db
async def test_a_line_stopped_mid_call_is_ledgered_at_its_estimate(
    database: Database, settings: Settings
) -> None:
    hanging = Speaker(turbo=-1)
    call = asyncio.create_task(
        _perform(deps(database, settings, hanging), PRICES, "[sighs] ну", "lily", ELEVEN)
    )
    await hanging.started.wait()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert await creator_calls(database) == [(TURBO, "creator", None, 10, Decimal("0.0002"))]


def test_each_line_goes_to_the_first_model_able_to_act_it(settings: Settings) -> None:
    speech, pin = settings.speech, settings.model_pins[ModelRole.SPEECH]
    assert pin.models == (GEMINI, TURBO, FULL)
    assert voicing(speech, pin, "Bob, that's the third time.") == (GEMINI, TURBO, FULL)
    assert voicing(speech, pin, "[whispers] Nobody tell Anna. [strong French accent] Oui.")[0] == GEMINI
    assert voicing(speech, pin, "[whispers] Nobody tell Anna... [giggles]") == ELEVEN  # a sound
    assert voicing(speech, pin, "[fart]") == ELEVEN  # nothing to say without the sound
    assert voicing(speech, pin, "Ну конечно, Миша опять забыл проектор.") == ELEVEN  # Cyrillic
    assert voicing(speech, pin, "[шёпотом] Hi") == ELEVEN  # a tag it doesn't know is taken as a sound
    no_sounds = SpeechModel(price=1, sounds=False)
    assert can_voice(no_sounds, "[SHOUTING] who did this") and not can_voice(no_sounds, "[applause] wow")
    assert can_voice(SpeechModel(price=1), "[fart]")  # an inline model may act a sound alone


def test_a_model_that_would_read_tags_aloud_gets_them_as_a_delivery_note(settings: Settings) -> None:
    gemini, turbo = settings.speech.model(GEMINI), settings.speech.model(TURBO)
    script = "[whispers]  Nobody tell Anna...  [sarcastic] Great cake."
    assert line_for(turbo, script, "lily") == Line(script, "lily")
    assert line_for(gemini, script, "lily") == Line(
        "Nobody tell Anna... Great cake.",
        "Despina",
        "Act it as a velvety, theatrical woman; whispers; sarcastic",
    )
    assert line_for(gemini, "Hello.", "bill") == Line("Hello.", "Charon", "Act it as a wise, grumpy old man")
    unknown = settings.speech.model("acme/tts")  # trusted with everything, at the dearest list price
    assert unknown.tags == "inline" and unknown.sounds and unknown.price == 80.0


async def test_a_line_is_held_at_the_first_able_models_price_and_dearer_fallbacks_only_if_they_fit(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    held: list[int] = []
    left: list[int | None] = [30]

    async def heavy(ctx: object, *, price: int) -> None:
        held.append(price)

    async def hold(ctx: object, *, next_turn: bool) -> int | None:
        return left[0]

    monkeypatch.setattr(voice, "heavy_allowed", heavy)
    monkeypatch.setattr(voice, "hold_left", hold)
    ctx = cast(Any, SimpleNamespace(deps=SimpleNamespace(settings=settings)))
    # 330 characters: about 8 Derpcoins on Turbo, 32 on v4. Holding the line at v4 refused it outright.
    assert await affordable(ctx, PRICES, ELEVEN, 330) == (TURBO,)
    assert held == [settings.credits_for_usd(float(PRICES.usd(TURBO, 330)))]
    left[0] = None  # a run that holds nothing: every fallback stays
    assert await affordable(ctx, PRICES, ELEVEN, 330) == ELEVEN


def test_casting_tags_count_as_directions_but_sounds_do_not() -> None:
    assert is_direction("elderly grandmother voice, croaky") and is_direction("Strong French accent")
    assert is_direction("SHOUTING") and is_direction("slow, trembling")
    assert not is_direction("squeaky horn") and not is_direction("croaky, laughs")


def test_a_skits_turns_are_joined_into_one_recording() -> None:
    one, two = as_wav(b"\x01\x00" * 100), as_wav(b"\x02\x00" * 50)
    with wave.open(io.BytesIO(joined([one, two]))) as file:
        assert file.getnframes() == 150 and file.getframerate() == 24000
    tagged = b"ID3\x04\x00\x00\x00\x00\x00\x02ab"  # a 2-byte ID3v2 tag, then the frames
    assert joined([tagged + b"one", tagged + b"two"]) == tagged + b"one" + b"two"  # later tags dropped
    assert joined([one]) is one
