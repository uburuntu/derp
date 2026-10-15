"""Agent run records: start, outputs, finish (usage, cost, provider calls, history)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelResponse, ToolCallPart
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.agent.models import AgentRun, RunOutput
from derp.inference.ledger import record_usage
from derp.inference.openrouter import response_usage


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
