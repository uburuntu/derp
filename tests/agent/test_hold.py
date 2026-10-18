"""One measure of a run's money cap: the guard, the stream's stop and web search all read ``hold_left``."""

from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from derp.agent.deps import RunDeps
from derp.agent.guard import Guard
from derp.agent.outputs import Kept, Made, Pending, Refused, afford, hold_left
from derp.agent.runs import start_run
from derp.agent.tools.web import search_budget
from derp.config import Settings
from derp.db.engine import Database
from derp.inference.api import GeneratedImage
from derp.inference.ledger import record_usage, simple_usage
from derp.inference.roles import RolePin
from derp.money.funding import Funding, Funds
from derp.money.models import RunFee


def response(cost: float) -> ModelResponse:
    return ModelResponse(parts=[TextPart("…")], provider_details={"cost": cost})


async def context(
    database: Database, settings: Settings, held: int, *, free: bool = False
) -> RunContext[RunDeps]:
    """A paid run that drew an image ($0.04), paid a fee (2), keeps a result carrying an image ($0.01)
    and a fee, and made one model turn ($0.01) after a window whose replayed turns aren't its own."""
    async with database.transaction() as session:
        run = await start_run(session, chat_id=-1, user_id=1, trigger="message", role="chat", funding="paid")
        record_usage(
            session, simple_usage("img", Decimal("0.04")), kind="image", funding="paid", run_id=run.id
        )
        session.add(RunFee(run_id=run.id, chat_id=-1, user_id=1, item="cutout", funding="paid", credits=2))
    image = GeneratedImage(b"", "image/png", "img", Decimal("0.01"))
    kept = Kept("r1", Made("photo", b"", "derp.png", "image", Pending(calls=[image], fees=["cutout"])))
    deps = SimpleNamespace(
        db=database, run_id=run.id, settings=settings, history_start=1, kept={"r1": kept},
        funds=Funds(Funding.FREE, "turn:1", None, None, 0) if free else Funds(Funding.PAID, "turn:1", 1, 1, held),
        funding=Funding.FREE if free else Funding.PAID,
    )  # fmt: skip
    return cast(
        RunContext[RunDeps],
        SimpleNamespace(deps=deps, messages=[response(1.0), response(0.01)], usage=RunUsage(requests=1)),
    )


@pytest.mark.db
async def test_the_hold_counts_side_calls_fees_turns_and_kept_results(
    database: Database, settings: Settings
) -> None:
    ctx = await context(database, settings, held=200)
    # $0.06 of provider calls → 72 credits, the ledgered fee 2, the kept fee 2 (effect_credits)
    assert await hold_left(ctx) == 200 - 72 - 2 - 2
    assert await hold_left(ctx, next_turn=True) == 124 - 12  # one more turn like the latest ($0.01)
    assert await search_budget(ctx) == 2  # a search fee is 2 credits: the per-request cap

    free = await context(database, settings, held=0, free=True)
    assert await hold_left(free) is None and await search_budget(free) is None  # no hold, no web


@pytest.mark.db
async def test_searches_go_before_the_answer_does(database: Database, settings: Settings) -> None:
    guard = Guard(
        run_id=1, pin=RolePin(model="a/b"), model_factory=lambda _pin: TestModel(), tool_calls=9, requests=9,
        deadline=1e12,
    )  # fmt: skip
    room_for_a_turn = await context(database, settings, held=76 + 12 + 1)
    assert await search_budget(room_for_a_turn) == 0  # the guard says search is out of budget
    assert not await guard.check(room_for_a_turn)  # …but the run may still use tools
    spent = await context(database, settings, held=76 + 12)
    assert await guard.check(spent)  # one more turn would reach the hold: wrap up


@pytest.mark.db
async def test_side_work_leaves_a_turn_to_talk_about_it_unless_its_price_was_agreed(
    database: Database, settings: Settings
) -> None:
    ctx = await context(database, settings, held=200)  # 124 left; 112 after one more turn
    await afford(ctx, 112, next_turn=True)
    with pytest.raises(Refused, match="about 113"):
        await afford(ctx, 113, next_turn=True)
    await afford(ctx, 124, next_turn=False)  # an approved image: its run was funded for it alone
    with pytest.raises(Refused):
        await afford(ctx, 125, next_turn=False)
    await afford(await context(database, settings, held=0, free=True), 10**6, next_turn=True)  # no hold
