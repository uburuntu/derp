"""Who pays for a run, and holding the money before it starts.

Order — private chat: the user's paid credits (plan allowance, purchases,
grants) → the user's sponsored daily allowance. Group: the chat's credits →
the invoker's paid credits → the invoker's sponsored allowance. The payer is
fixed when the run reserves; everything the run does counts against that
reservation. Paid runs use the standard chat model, sponsored runs the cheap
one, and sponsorship also stops at a global daily dollar cap.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import User
from derp.inference.ledger import estimate_cost
from derp.inference.models import ModelPin, ProviderCall
from derp.inference.roles import ModelRole
from derp.money.models import RunFee, Wallet
from derp.money.wallets import balance, capture, credit, release, reserve, wallet

PAID_SOURCES = ("plan", "purchase", "grant", "migration")
TYPICAL_OUTPUT_TOKENS = 300


class Funding(StrEnum):
    PAID = "paid"
    SPONSORED = "sponsored"
    FREE = "free"

    @property
    def role(self) -> ModelRole:
        return {
            Funding.PAID: ModelRole.CHAT,
            Funding.SPONSORED: ModelRole.CHAT_SPONSORED,
            Funding.FREE: ModelRole.CHAT_FREE,
        }[self]


@dataclass(frozen=True, slots=True)
class Funds:
    funding: Funding
    operation_id: str
    wallet_id: int | None  # None: nothing reserved (free models)
    reservation_id: int | None
    credits: int  # held for this run; the run's hard money cap


Notify = Callable[[str], Awaitable[None]]


async def _quiet(_text: str) -> None:
    return None


def day_end(now: datetime) -> datetime:
    return datetime.combine(now.date() + timedelta(days=1), time(), tzinfo=UTC)


def sponsored_lot_id(user_id: int, now: datetime) -> str:
    """Operation id of a user's sponsored allowance for one UTC day."""
    return f"sponsored:{user_id}:{now.date().isoformat()}"


async def sponsored_spent_today(session: AsyncSession, now: datetime) -> Decimal:
    start = datetime.combine(now.date(), time(), tzinfo=UTC)
    spent = await session.scalar(
        select(func.coalesce(func.sum(ProviderCall.cost_usd), 0)).where(
            ProviderCall.funding == "sponsored", ProviderCall.created_at >= start
        )
    )
    return Decimal(spent or 0)


class Funder:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        notify: Notify = _quiet,
        prefix: Mapping[Funding, int] | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.notify = notify
        self.prefix = prefix or {}  # tokens every request carries before the window (persona, tool schemas)
        self._cap_reported: date | None = None

    def window(self, funding: Funding) -> int:
        windows = self.settings.context_windows
        return {Funding.PAID: windows.paid, Funding.SPONSORED: windows.sponsored, Funding.FREE: windows.free}[
            funding
        ]

    async def typical_turn(self, session: AsyncSession, funding: Funding) -> int:
        """Credits a plain turn usually costs on this tier's model: the least worth starting a run for.

        Input is the prompt prefix (persona, tool schemas) plus half the window, priced uncached.
        """
        prices = await session.get(ModelPin, funding.role.value)
        if prices is None or prices.price_prompt is None:
            return 1
        usd = estimate_cost(
            self.prefix.get(funding, 0) + self.window(funding) // 2,
            TYPICAL_OUTPUT_TOKENS,
            float(prices.price_prompt),
            float(prices.price_completion or 0),
        )
        return max(1, self.settings.credits_for_usd(float(usd)))

    async def reserve(
        self,
        *,
        chat_id: int,
        private: bool,
        user_id: int,
        operation_id: str,
        free_allowed: bool = False,
        paid_minimum: int | None = None,
        now: datetime | None = None,
    ) -> Funds | None:
        """Hold money for a run from the first payer that can cover a typical turn.

        Free models (nothing held) come last and only when allowed (consent / admin opt-in);
        None means nobody can pay. ``paid_minimum`` funds an action of known price: only paid
        Derpcoins, from the first payer whose balance covers it.
        """
        moment = now or datetime.now(UTC)
        cap = self.settings.run_caps.credits
        cap_hit = False
        async with self.db.transaction() as session:
            payers = [] if private else [(await wallet(session, "chat", chat_id)).id]
            mine = (await wallet(session, "user", user_id)).id
            person = await session.get(User, user_id)
            if private or person is None or person.group_spending:
                payers.append(mine)
            minimum = paid_minimum or await self.typical_turn(session, Funding.PAID)
            for wallet_id in payers:
                held = await reserve(
                    session, wallet_id, operation_id, cap, sources=PAID_SOURCES, minimum=minimum, now=moment
                )
                if held is not None:
                    return Funds(Funding.PAID, operation_id, wallet_id, held.id, held.amount)
            if paid_minimum is not None:
                return None
            if await sponsored_spent_today(session, moment) >= Decimal(
                str(self.settings.sponsored_global_daily_usd)
            ):
                cap_hit = True
            else:
                await credit(
                    session,
                    mine,
                    sponsored_lot_id(user_id, moment),
                    self.settings.sponsored_daily_credits,
                    "sponsored",
                    expires_at=day_end(moment),
                )
                held = await reserve(
                    session,
                    mine,
                    operation_id,
                    cap,
                    sources=("sponsored",),
                    minimum=await self.typical_turn(session, Funding.SPONSORED),
                    now=moment,
                )
                if held is not None:
                    return Funds(Funding.SPONSORED, operation_id, mine, held.id, held.amount)
        if cap_hit and self._cap_reported != moment.date():
            self._cap_reported = moment.date()
            await self.notify(
                f"Sponsored usage hit today's ${self.settings.sponsored_global_daily_usd:g} cap."
            )
        return Funds(Funding.FREE, operation_id, None, None, 0) if free_allowed else None

    async def ran_dry(self, wallet_id: int, *, now: datetime | None = None) -> bool:
        """After a charge: True once when a chat wallet can no longer pay a typical paid turn.

        Re-armed when the wallet can pay again (a top-up also clears it). Its own short transaction,
        after the charge committed; the caller posts the notice afterwards, outside any transaction.
        """
        async with self.db.transaction() as session:
            row = await session.get(Wallet, wallet_id, with_for_update=True)
            if row is None or row.kind != "chat":
                return False
            left = await balance(session, wallet_id, sources=PAID_SOURCES, now=now)
            if left >= await self.typical_turn(session, Funding.PAID):
                row.empty_noticed_at = None
                return False
            if row.empty_noticed_at is not None:
                return False
            row.empty_noticed_at = now or datetime.now(UTC)
            return True

    async def settle(self, session: AsyncSession, funds: Funds, run_id: int, *, delivered: bool) -> int:
        """Charge the run's marked-up provider cost plus its fees if Telegram accepted an output, else return the hold."""
        if funds.reservation_id is None:
            return 0
        if not delivered:
            await release(session, funds.operation_id)
            return 0
        cost = await run_cost(session, run_id)
        price = self.settings.credits_for_usd(float(cost)) + await run_fees(session, run_id)
        return await capture(session, funds.operation_id, price)


async def run_fees(session: AsyncSession, run_id: int) -> int:
    """Flat Derp fees (credits) a run has incurred."""
    total = await session.scalar(
        select(func.coalesce(func.sum(RunFee.credits), 0)).where(RunFee.run_id == run_id)
    )
    return int(total or 0)


async def run_cost(session: AsyncSession, run_id: int) -> Decimal:
    """Everything the provider ledger attributes to a run: model turns, image notes, tools."""
    total = await session.scalar(
        select(func.coalesce(func.sum(ProviderCall.cost_usd), 0)).where(ProviderCall.run_id == run_id)
    )
    return Decimal(total or 0)
