"""Replay a chat's real runs through context-window strategies and measure what a prompt cache could serve.

Runs where the bot runs (it reads the chat's stored history, read-only), feeding the script on stdin:

    ssh <prod host> 'docker exec -i derp python - --chat CHAT_ID --days 14 \\
        --strategy prod --strategy before:idle=inf,card=0' < tools/context/simulate.py > runs.jsonl

For each run of the chat (oldest first) and each strategy, the real ``build_window`` builds the window
the run would have seen, starting where that strategy kept the topic's window, with the chat card
(``persona.chat_card``) at its head; the request is approximated as its messages in order, as a
provider renders them. Each line records the window's estimated tokens (card included), the prefix it
shares with the previous request of the same topic and funding tier (what a warm cache serves), the
run's situation and current message, the gap since that request, the run's own later steps (tool
rounds, from its stored history), how many messages and hours the window covers, and how often the
run looked further back with history tools. The first line is metadata: the tiers' static prefix
(persona and tool schemas) and budgets. Only numbers are printed, never chat content. ``analyze.py``
turns the lines into cache shares and costs.

Strategy specs are ``name[:key=value,...]``. Every key defaults to production's policy
(``agent/context.py``, ``agent/runtime.py``), so ``prod`` alone is what runs today; keep the defaults
in step when that policy changes:
  budget=1.0       fresh window size, a multiple of the tier's ``context_windows`` budget
  slack=1.5        a kept window may grow to this multiple of the budget before starting afresh
  idle=10          minutes of quiet after which the window starts afresh (``inf``: never)
  idle_budget=0.5  the fresh size after a pause, of the tier's budget (free runs keep the whole budget)
  card=1           the chat card heads the window; 0 puts it in the per-run situation instead
  summary=TOK      a compaction block of this many tokens heads the window, rewritten at every restart
                   from the window it replaces (``compacted``: what that call reads); not in production
"""

import argparse
import asyncio
import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from os.path import commonprefix
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from derp.agent import context
from derp.agent.models import AgentRun
from derp.agent.persona import chat_card
from derp.agent.prefix import prefixes
from derp.agent.runtime import EARLIER_DAYS
from derp.agent.tools import FAMILIES
from derp.agent.tools.memory import family as memory_family
from derp.config import get_settings
from derp.history.digests import latest_before
from derp.history.models import Chat, Message
from derp.history.render import chat_time
from derp.memory.notes import chat_notes, load_memory
from derp.money.funding import Funding

SERIALIZED = {"user": "\x01U", "tool": "\x01T", "retry": "\x01R", "assistant": "\x01A", "summary": "\x01S"}
LOOKUPS = {"search_history", "read_messages", "chat_query", "digest"}  # tools that reach past the window
# What runs stored before the chat card carried in their situation; the card stands for it now.
CARD_PARTS = re.compile(
    r"<chat_profile .*?</chat_profile>\n*|<digest .*?</digest>\n*|Chat notes[^\n]*:\n(?:- note [^\n]*\n?)*",
    re.DOTALL,
)


def tokens(text: str) -> int:
    return len(text.encode()) // 4  # the estimate budgets use (history.render.estimate_tokens)


def serialize(messages: Sequence[ModelMessage]) -> str:
    """Messages in the order a chat template renders them; equal prefixes here are equal prompt prefixes."""
    out: list[str] = []
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart):
                    text = part.content if isinstance(part.content, str) else " ".join(map(str, part.content))
                    out.append(SERIALIZED["user"] + text)
                elif isinstance(part, ToolReturnPart):
                    out.append(SERIALIZED["tool"] + part.tool_call_id + part.model_response_str())
                elif isinstance(part, RetryPromptPart):
                    out.append(SERIALIZED["retry"] + str(part.tool_call_id) + str(part.content))
        else:
            text = "".join(p.content for p in message.parts if isinstance(p, TextPart))
            calls = "".join(
                p.tool_name + p.args_as_json_str() for p in message.parts if isinstance(p, ToolCallPart)
            )
            out.append(SERIALIZED["assistant"] + text + calls)
    return "".join(out)


def steps_of(history: Sequence[ModelMessage]) -> tuple[int, list[int], int]:
    """A run's situation + current message (without what the chat card holds now), what each later model
    call added (the reply before it and the tool results), and its number of model calls."""
    if not history:
        return 0, [], 0
    first = history[0]
    situation = tokens(CARD_PARTS.sub("", serialize([first]))) if isinstance(first, ModelRequest) else 0
    added: list[int] = []
    calls = pending = 0
    for message in history[1:]:
        if isinstance(message, ModelResponse):
            calls += 1
            if calls > 1:
                added.append(pending)
            pending = tokens(serialize([message]))
        else:
            pending += tokens(serialize([message]))
    return situation, added, calls


@dataclass
class Topic:
    start: int | None = None
    last_at: datetime | None = None
    last_request: str = ""
    summary_of: int | None = None


@dataclass
class Strategy:
    name: str
    budget: float = 1.0
    slack: float = context.STICKY_SLACK
    idle: float = context.IDLE_SECONDS / 60
    idle_budget: float = context.IDLE_SHARE
    card: float = 1
    summary: int = 0
    topics: dict[tuple[int | None, str], Topic] = field(default_factory=dict[tuple[int | None, str], Topic])

    @classmethod
    def parse(cls, spec: str) -> Strategy:
        name, _, rest = spec.partition(":")
        values: dict[str, Any] = {}
        for item in filter(None, rest.split(",")):
            key, _, value = item.partition("=")
            values[key] = int(value) if key == "summary" else float(value)
        return cls(name=name, **values)


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--chat", type=int, required=True)
    parser.add_argument("--days", type=float, default=14)
    parser.add_argument("--strategy", action="append", default=[], help="name[:key=value,...]; repeatable")
    args = parser.parse_args()
    strategies = [Strategy.parse(spec) for spec in args.strategy or ["prod"]]
    settings = get_settings()
    budgets = settings.context_windows.model_dump()
    engine = create_async_engine(
        settings.database_url, connect_args={"server_settings": {"default_transaction_read_only": "on"}}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    # The same run is replayed in many windows: load each stored history once.
    load = context.load_messages
    runs_for = context._runs_for
    loaded: dict[int, AgentRun] = {}
    histories: dict[int, list[ModelMessage]] = {}

    async def shared_runs(session: AsyncSession, chat_id: int, message_ids: list[int]) -> dict[int, AgentRun]:
        found = await runs_for(session, chat_id, message_ids)
        return {message_id: loaded.setdefault(run.id, run) for message_id, run in found.items()}

    def shared_load(data: list[Any] | None) -> list[ModelMessage]:
        key = id(data)  # the shared runs hand over the same stored list every time
        if key not in histories:
            histories[key] = load(data)
        return histories[key]

    context._runs_for = shared_runs
    context.load_messages = shared_load

    since = datetime.now(UTC) - timedelta(days=args.days)
    async with sessions() as session:
        chat = await session.get(Chat, args.chat)
        if chat is None:
            raise SystemExit("no such chat")
        tz = chat_time(chat)
        memory = await load_memory(session, chat.id, 0)  # today's chat notes stand in for each run's
        runs = list(
            await session.scalars(
                select(AgentRun)
                .where(
                    AgentRun.chat_id == chat.id,
                    AgentRun.created_at >= since,
                    AgentRun.trigger.in_(("message", "command")),
                    AgentRun.trigger_message_id.is_not(None),
                )
                .order_by(AgentRun.id)
            )
        )
    prefix = prefixes(FAMILIES)
    meta = {
        "chat": chat.id,
        "runs": len(runs),
        "prefix": {funding.value: prefix[funding] for funding in Funding},
        "budgets": budgets,
    }
    print(json.dumps({"meta": meta}), flush=True)

    scan = context.SCAN_LIMIT
    for run in runs:
        try:
            history = load(run.messages) if run.messages else []
        except ValueError:
            history = []
        situation, added, calls = steps_of(history)
        lookups = sum(
            isinstance(part, ToolCallPart) and part.tool_name in LOOKUPS
            for message in history
            if isinstance(message, ModelResponse)
            for part in message.parts
        )
        funding = Funding(run.funding)
        mode = chat.notes_mode if funding in memory_family.tiers else None
        async with sessions() as session:
            for strategy in strategies:
                topic = strategy.topics.setdefault((run.thread_id, run.funding), Topic())
                gap = (run.created_at - topic.last_at).total_seconds() / 60 if topic.last_at else None
                scale = strategy.budget
                if gap is None or gap >= strategy.idle:  # a pause (or the topic's first run): start afresh
                    topic.start = None
                    if not math.isinf(strategy.idle) and funding is not Funding.FREE:
                        scale = strategy.idle_budget
                budget = int(budgets[run.funding] * scale)
                context.STICKY_SLACK = strategy.slack * strategy.budget / scale
                context.SCAN_LIMIT = int(scan * max(1.0, budget / budgets["paid"]))
                window = await context.build_window(
                    session,
                    chat_id=chat.id,
                    thread_id=run.thread_id,
                    before_message_id=run.trigger_message_id or 0,
                    budget=budget,
                    tz=tz,
                    start_at=topic.start,
                )
                restarted = window.oldest_message_id != topic.start
                oldest = (
                    await session.get(Message, (chat.id, window.oldest_message_id))
                    if window.oldest_message_id
                    else None
                )
                earlier = await latest_before(
                    session,
                    chat.id,
                    run.thread_id or 0,
                    window.oldest_message_id or run.trigger_message_id or 0,
                )
                if earlier is not None and earlier.day < run.created_at.astimezone(tz).date() - timedelta(
                    days=EARLIER_DAYS
                ):
                    earlier = None
                card = chat_card(chat=chat, notes=chat_notes(memory, mode), earlier=earlier) or ""
                topic.start = window.oldest_message_id
                request = serialize(window.messages)
                if strategy.card and card:
                    request = SERIALIZED["user"] + card + request
                compacted = 0  # tokens a compaction call reads to rewrite the summary: what it replaces
                if strategy.summary:
                    if restarted or topic.summary_of is None:
                        topic.summary_of = window.oldest_message_id
                        compacted = tokens(topic.last_request)
                    head = f"{SERIALIZED['summary']}{topic.summary_of}"
                    request = head + "·" * max(0, strategy.summary * 4 - len(head)) + request
                shared = tokens(commonprefix([topic.last_request, request]))  # noqa: RUF071 - by character
                replayed = sum(
                    tokens(serialize([m])) for m in window.messages if isinstance(m, ModelResponse)
                )
                row = {
                    "s": strategy.name,
                    "run": run.id,
                    "at": run.created_at.isoformat(),
                    "topic": run.thread_id,
                    "tier": run.funding,
                    "gap": gap,
                    "window": tokens(request),
                    "shared": shared,
                    "restart": restarted,
                    "card": tokens(card),
                    "replayed": replayed,
                    "count": window.count,
                    "span": round((run.created_at - oldest.date).total_seconds() / 3600, 2) if oldest else 0,
                    "lookups": lookups,
                    "situation": situation + (0 if strategy.card else tokens(card)),
                    "steps": added,
                    "calls": calls,
                    "compacted": compacted,
                    "summary": strategy.summary if compacted else 0,
                }
                print(json.dumps(row), flush=True)
                topic.last_request = request
                topic.last_at = run.created_at
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
