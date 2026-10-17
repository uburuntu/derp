"""Agent run records: start, outputs, finish (usage, cost, provider calls, history)."""

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from sqlalchemy import null, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from derp.agent.models import AgentRun, RunOutput
from derp.config import Retention
from derp.db.engine import Database
from derp.history.render import estimate_tokens
from derp.history.retention import expired
from derp.inference.api import OpenRouterAPI, OpenRouterError
from derp.inference.ledger import cancelled_cost, estimate_cost, record_usage
from derp.inference.models import ModelPin
from derp.inference.openrouter import response_usage, responses
from derp.inference.roles import ModelRole


@dataclass(frozen=True, slots=True)
class RunTotals:
    cost_usd: Decimal
    input_tokens: int
    output_tokens: int
    tool_calls: int
    served_model: str | None


def serialize_messages(messages: Sequence[ModelMessage]) -> list[Any]:
    """Pydantic AI messages as JSON, with inline binary content replaced by a text marker."""
    dumped: list[Any] = ModelMessagesTypeAdapter.dump_python(list(messages), mode="json")
    return [_strip_binary(message) for message in dumped]


def _strip_binary(value: Any) -> Any:
    if isinstance(value, dict):
        if value.get("kind") == "binary":  # pyright: ignore[reportUnknownMemberType]
            return f"[{value.get('media_type', 'binary')} omitted]"  # pyright: ignore[reportUnknownMemberType]
        return {key: _strip_binary(item) for key, item in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, list):
        return [_strip_binary(item) for item in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def load_messages(data: list[Any] | None) -> list[ModelMessage]:
    return ModelMessagesTypeAdapter.validate_python(data or [])


def totals(messages: Sequence[ModelMessage]) -> RunTotals:
    responses = [m for m in messages if isinstance(m, ModelResponse)]
    usages = [response_usage(r) for r in responses]
    return RunTotals(
        cost_usd=sum((u.cost_usd for u in usages), Decimal(0)),
        input_tokens=sum(u.input_tokens for u in usages),
        output_tokens=sum(u.output_tokens for u in usages),
        tool_calls=sum(isinstance(p, ToolCallPart) for r in responses for p in r.parts),
        served_model=next((u.model for u in reversed(usages) if u.model), None),
    )


async def price_unreported(
    db: Database,
    messages: Sequence[ModelMessage],
    role: ModelRole,
    context_tokens: int,
    *,
    api: OpenRouterAPI | None = None,
) -> None:
    """Give every paid response that came back without a cost one, so none is ledgered as free.

    Streams cut off by stop or timeout never get their usage chunk, and a completed response
    may lack ``usage.cost`` too. OpenRouter is asked by generation id (when ``api`` is given);
    otherwise the cost is estimated from the response's tokens (``context_tokens`` and the
    written text when it has none) at the pin's price snapshot. Free models stay free.
    """
    if role is ModelRole.CHAT_FREE:
        return
    unpriced = [
        m for m in messages if isinstance(m, ModelResponse) and (m.provider_details or {}).get("cost") is None
    ]
    if not unpriced:
        return
    async with db.session() as session:
        prices = await session.get(ModelPin, role.value)
    for response in unpriced:
        cost: Decimal | None = None
        if api is not None and response.provider_response_id:
            with contextlib.suppress(OpenRouterError):
                cost = await cancelled_cost(api, response.provider_response_id)
        if cost is None and prices is not None and prices.price_prompt is not None:
            written = sum(estimate_tokens(p.content) for p in response.parts if isinstance(p, TextPart))
            cost = estimate_cost(
                response.usage.input_tokens or context_tokens,
                response.usage.output_tokens or written,
                float(prices.price_prompt),
                float(prices.price_completion or 0),
            )
        if cost is not None:
            response.provider_details = {
                **(response.provider_details or {}),
                "cost": float(cost),
                "estimated": True,
            }


async def ledger_side_call(
    db: Database,
    messages: Sequence[ModelMessage],
    *,
    kind: str,
    funding: str,
    role: ModelRole,
    context_tokens: int,
    run_id: int | None = None,
    chat_id: int | None = None,
    user_id: int | None = None,
    requested_model: str | None = None,
) -> list[ModelResponse]:
    """Ledger a model call outside a run's own turns (media notes, digests, scout probes).

    Every response is ledgered, failed and empty ones included, after ``price_unreported``
    gave each paid one a cost. Returns the responses.
    """
    paid = responses(messages)
    if not paid:
        return paid
    await price_unreported(db, paid, role, context_tokens)
    async with db.transaction() as session:
        for response in paid:
            record_usage(
                session,
                response_usage(response),
                kind=kind,
                funding=funding,
                role=role.value,
                run_id=run_id,
                chat_id=chat_id,
                user_id=user_id,
                requested_model=requested_model,
            )
    return paid


async def start_run(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
    trigger: str,
    role: str,
    funding: str,
    thread_id: int | None = None,
    trigger_message_id: int | None = None,
    wallet_id: int | None = None,
    reservation_id: int | None = None,
) -> AgentRun:
    run = AgentRun(
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
        trigger=trigger,
        trigger_message_id=trigger_message_id,
        role=role,
        funding=funding,
        wallet_id=wallet_id,
        reservation_id=reservation_id,
        status="running",
    )
    session.add(run)
    await session.flush()
    return run


def record_output(session: AsyncSession, run: AgentRun, message_id: int, kind: str) -> None:
    session.add(RunOutput(run_id=run.id, chat_id=run.chat_id, message_id=message_id, kind=kind))


def finish_run(
    session: AsyncSession,
    run: AgentRun,
    messages: Sequence[ModelMessage],
    *,
    status: str,
    context_tokens: int = 0,
    error_type: str | None = None,
) -> RunTotals:
    """Close a run: aggregate usage, ledger each provider response, keep the new history."""
    result = totals(messages)
    for response in (m for m in messages if isinstance(m, ModelResponse)):
        record_usage(
            session,
            response_usage(response),
            kind="chat",
            funding=run.funding,
            role=run.role,
            run_id=run.id,
            chat_id=run.chat_id,
            user_id=run.user_id,
        )
    usages = [response_usage(m) for m in messages if isinstance(m, ModelResponse)]
    run.status = status
    run.messages = serialize_messages(messages)
    run.served_model = result.served_model
    run.context_tokens = context_tokens
    run.input_tokens = result.input_tokens
    run.output_tokens = result.output_tokens
    run.cache_read_tokens = sum(u.cache_read_tokens for u in usages)
    run.reasoning_tokens = sum(u.reasoning_tokens for u in usages)
    run.tool_calls = result.tool_calls
    run.cost_usd = result.cost_usd
    run.error_type = error_type
    run.finished_at = datetime.now(UTC)
    return result


async def run_for_output(session: AsyncSession, chat_id: int, message_id: int) -> AgentRun | None:
    """The run behind a delivered message (``/info`` as a reply)."""
    return await session.scalar(
        select(AgentRun)
        .join(RunOutput, RunOutput.run_id == AgentRun.id)
        .where(RunOutput.chat_id == chat_id, RunOutput.message_id == message_id, RunOutput.kind != "reaction")
        .order_by(AgentRun.id.desc())
        .limit(1)
    )


async def scrub_runs(session: AsyncSession, chat_id: int, *, since: datetime | None = None) -> None:
    """Drop stored run transcripts of a chat (they quote its messages); usage and charges stay."""
    where = [AgentRun.chat_id == chat_id, AgentRun.messages.is_not(None)]
    if since is not None:
        where.append(AgentRun.created_at >= since)
    await session.execute(update(AgentRun).where(*where).values(messages=null()))


async def expire_run_messages(session: AsyncSession, retention: Retention, now: datetime) -> None:
    """Run transcripts follow their chat's retention, like the messages they quote."""
    old = expired(AgentRun.chat_id, AgentRun.created_at, retention, now)
    await session.execute(update(AgentRun).where(AgentRun.messages.is_not(None), old).values(messages=null()))
