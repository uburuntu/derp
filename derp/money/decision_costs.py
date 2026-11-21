"""Reserve the payer before a decision is sent, and settle even checks that match no rule."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.agent.models import AgentRun
from derp.config import Settings
from derp.inference.api import Decided
from derp.inference.ledger import record_usage, simple_usage
from derp.inference.models import ProviderCall
from derp.keys.models import ContributedKey, KeyChat
from derp.keys.routing import serving, spent
from derp.keys.vault import unseal
from derp.money.funding import PAID_SOURCES
from derp.money.models import Reservation, Wallet
from derp.money.wallets import capture, release, reserve, wallet


@dataclass(frozen=True)
class DecisionPayer:
    model: str
    rate: Decimal  # USD per million input tokens
    key_call: int | None = None
    sub_key: str | None = field(default=None, repr=False)
    reservation_id: int | None = None
    operation: str | None = None
    run_id: int | None = None

    def estimate(self, tokens: int) -> Decimal:
        return (self.rate * tokens / 1_000_000).quantize(Decimal("0.00000001"))


async def authorize(
    session: AsyncSession,
    settings: Settings,
    *,
    chat_id: int,
    user_id: int | None,
    model: str,
    upper_tokens: int,
    operation: str | None,
    key_id: int | None,
    group_run: int | None = None,
) -> DecisionPayer | None:
    """A rule screen uses the chat's first eligible OpenAI key, else its wallet. Ordinary decision
    calls keep their existing creator funding unless the running answer has an OpenAI key.
    """
    now = datetime.now(UTC)
    if operation or key_id is not None:
        keys = (
            select(ContributedKey, KeyChat.enabled_at)
            .join(KeyChat, KeyChat.key_id == ContributedKey.id)
            .where(
                KeyChat.chat_id == chat_id,
                KeyChat.state == "on",
                ContributedKey.provider == "openai",
                *serving(now),
            )
            .order_by(ContributedKey.id)  # consistent lock order even for keys shared between groups
            .with_for_update(of=ContributedKey)
        )
        if key_id is not None:
            keys = keys.where(ContributedKey.id == key_id)
        rows = (await session.execute(keys)).all()
        for key, _enabled in sorted(rows, key=lambda row: (row[1], row[0].id)):
            payer = DecisionPayer(
                settings.decisions.openai_model, Decimal(str(settings.decisions.openai_usd_per_m_input))
            )
            estimate = payer.estimate(upper_tokens)
            if await spent(session, key, now) + estimate <= key.cap_usd:
                # A concurrent screen, even in a different chat using this key, must see this commitment.
                # Replace the provisional estimate with provider usage after the request returns.
                call = record_usage(
                    session,
                    simple_usage(payer.model, Decimal(0), upstream_usd=estimate),
                    kind="decision",
                    funding="key",
                    role="rule_screen" if operation else "decision",
                    chat_id=chat_id,
                    user_id=user_id,
                    key_id=key.id,
                    run_id=group_run,
                )
                await session.flush()
                return DecisionPayer(
                    payer.model,
                    payer.rate,
                    call.id,
                    unseal(settings.keys_secret, key.sub_key),
                    run_id=group_run,
                )
        if key_id is not None and not operation:
            return None
    payer = DecisionPayer(model, Decimal(str(settings.decisions.price_usd_per_m_input)))
    if group_run is not None:
        # This automatic answer already holds shared coins. Its normal settlement includes this call;
        # a key-only answer with no shared hold skips the optional post-invocation optimization.
        held = await session.scalar(
            select(Reservation.amount)
            .join(AgentRun, AgentRun.reservation_id == Reservation.id)
            .join(Wallet, Wallet.id == Reservation.wallet_id)
            .where(
                AgentRun.id == group_run,
                AgentRun.chat_id == chat_id,
                Wallet.kind == "chat",
                Wallet.owner_id == chat_id,
                Reservation.state == "held",
            )
        )
        if held is None or held < settings.credits_for_usd(payer.estimate(upper_tokens)):
            return None
        return DecisionPayer(model, payer.rate, run_id=group_run)
    if operation is None:
        return payer
    cost = settings.credits_for_usd(payer.estimate(upper_tokens))
    shared = await wallet(session, "chat", chat_id)
    held = await reserve(session, shared.id, operation, cost, sources=PAID_SOURCES)
    if held is None:
        return None
    return DecisionPayer(model, payer.rate, reservation_id=held.id, operation=operation)


async def settle(
    session: AsyncSession,
    settings: Settings,
    payer: DecisionPayer,
    usage: Decided | None,
    *,
    chat_id: int,
    user_id: int | None,
    screening: bool,
) -> None:
    if payer.key_call is not None:
        call = await session.get(ProviderCall, payer.key_call, with_for_update=True)
        if call is None:
            raise LookupError("decision reservation is missing")
        if usage is None:
            await session.delete(call)
        else:
            call.model, call.cost_usd, call.upstream_usd = usage.model, usage.cost_usd, usage.upstream_usd
            call.input_tokens, call.cache_read_tokens = usage.input_tokens, usage.cached_tokens
        return
    if usage is None:
        if payer.operation:
            await release(session, payer.operation)
        return
    call = record_usage(
        session,
        simple_usage(usage.model, usage.cost_usd, input_tokens=usage.input_tokens),
        kind="decision",
        funding="paid" if screening or payer.run_id is not None else "creator",
        run_id=payer.run_id,
        role="rule_screen" if screening else "decision",
        chat_id=chat_id,
        user_id=user_id,
    )
    call.cache_read_tokens = usage.cached_tokens
    if payer.operation:
        call.reservation_id = payer.reservation_id
        await capture(session, payer.operation, settings.credits_for_usd(usage.cost_usd))
