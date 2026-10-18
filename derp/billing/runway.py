"""How long Derpcoins last at a chat's recent pace: the patron post's runway and the group pack menu.

The pace is a wallet's average daily charged spend (``agent_runs.wallet_id``, ``charged_credits``) over
the days since its first charge in the last ``PACE_DAYS``, so a young wallet isn't diluted. With fewer
than ``MIN_ACTIVE_DAYS`` days of spending there is no pace: callers fall back or say nothing.
"""

import math
from datetime import datetime, timedelta

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.agent.models import AgentRun
from derp.locales import t

PACE_DAYS = 14
MIN_ACTIVE_DAYS = 3
MONTH_DAYS = 30


async def daily_pace(session: AsyncSession, wallet_id: int, now: datetime) -> float | None:
    """Derpcoins the wallet's runs were charged per day lately; None with too little spending to tell."""
    charged = await session.execute(
        select(
            func.coalesce(func.sum(AgentRun.charged_credits), 0),
            func.count(func.distinct(cast(func.timezone("UTC", AgentRun.created_at), Date))),
            func.min(AgentRun.created_at),
        ).where(
            AgentRun.wallet_id == wallet_id,
            AgentRun.created_at >= now - timedelta(days=PACE_DAYS),
            AgentRun.charged_credits > 0,
        )
    )
    spent, active, first = charged.one()
    if active < MIN_ACTIVE_DAYS or first is None or spent <= 0:
        return None
    span = min(PACE_DAYS, max(active, math.ceil((now - first).total_seconds() / 86400)))
    return int(spent) / span


def lasts(locale: str, credits: int, pace: float) -> str:
    """How long ``credits`` last at ``pace`` a day, in words ("about **13 days**", "over a month")."""
    days = credits / pace
    if days > MONTH_DAYS:
        return t(locale, "runway.month")
    if days >= 2:
        return t(locale, "runway.days", days=round(days))
    return t(locale, "runway.day" if days >= 1 else "runway.hours")
