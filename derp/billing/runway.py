"""How long Derpcoins last at a chat's recent pace: the patron post's runway and the group pack menu.

The pace is a wallet's average daily settled spend on runs and standalone rule checks over
the days since its first charge in the last ``PACE_DAYS``, so a young wallet isn't diluted. With fewer
than ``MIN_ACTIVE_DAYS`` days of spending there is no pace: callers fall back or say nothing.
"""

import math
from datetime import datetime, timedelta

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.agent.models import AgentRun
from derp.inference.models import ProviderCall
from derp.locales import t
from derp.money.amounts import Amount
from derp.money.models import Reservation

PACE_DAYS = 14
MIN_ACTIVE_DAYS = 3
MONTH_DAYS = 30


async def daily_pace(session: AsyncSession, wallet_id: int, now: datetime) -> float | None:
    """Derpcoins this wallet spent per day lately, or None with too little spending to tell."""
    since = now - timedelta(days=PACE_DAYS)
    runs = select(AgentRun.charged_credits.label("dc"), AgentRun.created_at.label("at")).where(
        AgentRun.wallet_id == wallet_id, AgentRun.created_at >= since, AgentRun.charged_credits > 0
    )
    # Count the settlement once, regardless of the number of provider calls it covers.
    standalone = select(Reservation.captured, Reservation.settled_at).where(
        Reservation.wallet_id == wallet_id,
        Reservation.settled_at >= since,
        Reservation.captured > 0,
        select(ProviderCall.id)
        .where(ProviderCall.reservation_id == Reservation.id, ProviderCall.run_id.is_(None))
        .exists(),
    )
    events = runs.union_all(standalone).subquery()
    charged = await session.execute(
        select(
            func.sum(events.c.dc),
            func.count(func.distinct(cast(func.timezone("UTC", events.c.at), Date))),
            func.min(events.c.at),
        )
    )
    spent, active, first = charged.one()
    if active < MIN_ACTIVE_DAYS or first is None or spent <= 0:
        return None
    span = min(PACE_DAYS, max(active, math.ceil((now - first).total_seconds() / 86400)))
    return float(spent) / span


def lasts(locale: str, amount: Amount, pace: float) -> str:
    """How long ``amount`` Derpcoins last at ``pace`` a day, in words ("about **13 days**", "over a month")."""
    days = float(amount) / pace
    if days > MONTH_DAYS:
        return t(locale, "runway.month")
    if days >= 2:
        return t(locale, "runway.days", days=round(days))
    return t(locale, "runway.day" if days >= 1 else "runway.hours")
