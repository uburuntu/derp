"""Chat-knowledge tools: search, exact reads, structured queries, day digests and the chat profile.

Free on every tier, except that building a digest is a model call charged to the
run. Scope always comes from the run: the current chat, and the current forum
topic unless the model asks for the whole chat.
"""

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from pydantic_ai import BinaryContent, RunContext
from pydantic_ai.toolsets import FunctionToolset
from sqlalchemy import func, select

from derp.agent.deps import RunDeps
from derp.agent.digests import build
from derp.agent.media import CLIPS, IMAGE_LIKE, as_content, inspect, note_images, referenced_media
from derp.agent.toolsets import Family
from derp.history.digests import day_stats, is_current, scope_key
from derp.history.models import Digest, ForumTopic, Message, Poll
from derp.history.queries import ChatQuery, Scope, around, id_range, resolve_members, run_query, search
from derp.history.render import describe_time, load_views, render_transcript
from derp.money.funding import Funding, run_cost

log = logging.getLogger("derp.digests")
toolset = FunctionToolset[RunDeps]()


def _scope(ctx: RunContext[RunDeps], whole_chat: bool) -> Scope:
    deps = ctx.deps
    thread_id = deps.target.thread_id
    return Scope(deps.chat.id, thread_id, whole_chat=whole_chat or thread_id is None, tz=deps.tz)


async def _render(ctx: RunContext[RunDeps], rows: list[Message]) -> str:
    async with ctx.deps.db.session() as session:
        views = await load_views(session, sorted(rows, key=lambda r: r.message_id))
    return render_transcript(views, ctx.deps.tz)


@toolset.tool
async def search_history(
    ctx: RunContext[RunDeps],
    query: str,
    days: float | None = None,
    user: str | None = None,
    whole_chat: bool = False,
) -> str:
    """Search this chat's stored messages (text, voice transcripts, media descriptions).

    Args:
        query: Words to look for; quotes for phrases, `-word` to exclude.
        days: Only the last N days.
        user: Only messages from this person (name or @username).
        whole_chat: Search all forum topics instead of the current one.
    """
    since = datetime.now(UTC) - timedelta(days=days) if days else None
    async with ctx.deps.db.session() as session:
        user_ids = await resolve_members(session, ctx.deps.chat.id, [user]) if user else []
        if user and not user_ids:
            return f"No one called {user!r} has been seen in this chat."
        rows = await search(session, _scope(ctx, whole_chat), query, since=since, user_ids=user_ids)
    if not rows:
        return "No matching messages are stored (older messages may have expired)."
    return await _render(ctx, rows)


@toolset.tool
async def read_messages(
    ctx: RunContext[RunDeps],
    around_id: int | None = None,
    first_id: int | None = None,
    last_id: int | None = None,
) -> str:
    """Read exact messages: either the conversation around one message id, or an id range (max 100).

    Args:
        around_id: Show ~10 messages before and after this message id.
        first_id: Start of an id range.
        last_id: End of an id range.
    """
    async with ctx.deps.db.session() as session:
        if around_id is not None:
            rows = await around(session, ctx.deps.chat.id, around_id, before=10, after=10)
        elif first_id is not None and last_id is not None:
            rows = await id_range(session, _scope(ctx, True), first_id, last_id, limit=100)
        else:
            return "Give around_id, or both first_id and last_id."
    if not rows:
        return "Those messages are not stored."
    return await _render(ctx, rows)


def _poll_line(poll: Poll) -> str:
    known = poll.origin != "user" or poll.closed
    options = ", ".join(
        f"{o.get('text')}" + (f" ({o.get('voters', 0)})" if known else "") for o in poll.options
    )
    status = "closed" if poll.closed else "open"
    results = "" if known else " — results unknown (Telegram only reports results of Derp's own polls)"
    return f"#{poll.message_id} [{poll.type}, {status}, {poll.total_voters if known else '?'} voters] {poll.question}: {options}{results}"


@toolset.tool
async def chat_query(ctx: RunContext[RunDeps], query: ChatQuery) -> str:
    """Structured questions about this chat: counts, leaderboards, filters, polls, joins and leaves.

    Use presets for common questions; combine filters and group_by for the rest.
    """
    async with ctx.deps.db.session() as session:
        result = await run_query(session, _scope(ctx, query.whole_chat), query)
    match result.kind:
        case "messages":
            return await _render(ctx, result.messages) if result.messages else "No messages match."
        case "groups":
            if not result.groups:
                return "Nothing matches."
            return "\n".join(f"{label}: {value}" for label, value in result.groups)
        case "polls":
            return "\n".join(_poll_line(p) for p in result.polls) or "No polls are stored."
        case "events":
            lines = [f"{describe_time(e.at, ctx.deps.tz)} {name}: {e.kind}" for e, name in result.events]
            return "\n".join(lines) or "No membership changes are stored."


@toolset.tool
async def chat_info(ctx: RunContext[RunDeps]) -> str:
    """This chat's profile and what Derp keeps: description, members, admins, topics, context settings."""
    chat = ctx.deps.chat
    async with ctx.deps.db.session() as session:
        totals = (
            await session.execute(
                select(func.count(), func.min(Message.date)).where(
                    Message.chat_id == chat.id, Message.deleted_at.is_(None)
                )
            )
        ).one()
        stored, oldest = int(totals[0]), totals[1]
        topics = list(await session.scalars(select(ForumTopic.name).where(ForumTopic.chat_id == chat.id)))
    profile = chat.profile
    lines = [f"{chat.type} chat “{chat.title or 'private'}”"]
    if description := profile.get("description"):
        lines.append(f"description: {description}")
    if count := profile.get("member_count"):
        lines.append(f"members: {count}")
    if admins := profile.get("admins"):
        lines.append("admins: " + ", ".join(str(a.get("name")) for a in admins))
    if topics:
        lines.append("topics: " + ", ".join(topics))
    lines.append(
        f"ambient context: {'on' if chat.ambient or chat.type == 'private' else 'off (mentions only)'}"
    )
    if stored:  # min(date) exists whenever something is stored
        lines.append(f"stored messages: {stored}, since {describe_time(oldest, ctx.deps.tz)}")
    return "\n".join(lines)


@toolset.tool
async def inspect_media(ctx: RunContext[RunDeps], message_id: int) -> list[str | BinaryContent] | str:
    """Look at a stored message's picture (photo, sticker, image file, album) or a frame of its video/GIF/video message.

    Args:
        message_id: The message id shown as #id in the history.
    """
    deps = ctx.deps
    async with deps.db.session() as session:
        rows = await referenced_media(session, deps.chat.id, message_id, None, (*IMAGE_LIKE, *CLIPS))
    if not rows:
        return "That message has no picture or video I can open."
    inspected = await inspect(deps.gateway, rows, frames=deps.workers)
    if not inspected:
        return "That media can't be opened (too big, expired, or no picture in it)."
    await note_images(
        deps.db,
        deps.model,
        deps.pin,
        inspected,
        run_id=deps.run_id,
        funding=deps.funding.value,
        role=deps.funding.role,
        chat_id=deps.chat.id,
        user_id=deps.user_id,
    )
    notes = [
        f"#{item.row.message_id}{' (one frame)' if item.still else ''}: {item.row.description or item.row.type}"
        for item in inspected
    ]
    if not deps.pin.vision:
        return "\n".join(notes)
    return [*notes, *as_content(inspected)]


PAID_BUILDS = 7  # days a paid run may summarize per call; other runs build one


async def _affordable(deps: RunDeps) -> bool:
    """Side calls ledgered so far still fit under the run's hold."""
    if deps.funds.reservation_id is None:
        return True
    async with deps.db.session() as session:
        spent = await run_cost(session, deps.run_id)
    return deps.settings.credits_for_usd(float(spent)) < deps.funds.credits


@toolset.tool
async def digest(
    ctx: RunContext[RunDeps],
    period: Literal["today", "yesterday", "last_7_days"] | date = "today",
    whole_chat: bool = False,
) -> str:
    """Summaries of chat days (in the chat's time zone): what was discussed, decided and planned.

    Prefer this over reading transcripts for "what happened" questions; cite its #ids with read_messages.

    Args:
        period: today, yesterday, last_7_days, or one date (YYYY-MM-DD).
        whole_chat: Summarize all forum topics instead of the current one.
    """
    deps = ctx.deps
    today = datetime.now(deps.tz).date()
    match period:
        case "today":
            days = [today]
        case "yesterday":
            days = [today - timedelta(days=1)]
        case "last_7_days":
            days = [today - timedelta(days=n) for n in range(6, -1, -1)]
        case _:
            if period > today:
                return "That day hasn't happened yet."
            days = [period]
    scope = _scope(ctx, whole_chat)
    key = scope_key(scope)
    builds = PAID_BUILDS if deps.funding is Funding.PAID else 1
    sections: list[str] = []
    for day in days:
        async with deps.db.session() as session:
            stats = await day_stats(session, scope, day)
            cached = await session.get(Digest, (deps.chat.id, key, day)) if stats else None
        if stats is None:
            if len(days) == 1:
                sections.append(f"{day:%a %Y-%m-%d}: no stored messages.")
            continue
        found, note = cached, ""
        if not is_current(cached, stats) and builds > 0 and await _affordable(deps):
            builds -= 1
            try:
                found = await build(deps, scope, key, day)
            except Exception as error:  # noqa: BLE001 - fall back to the cached digest or a pointer
                log.info("digest build failed: %s", type(error).__name__)
        if found is not None and found.stale:  # it may quote what was edited or deleted since
            found = None
        elif found is not None and not is_current(found, stats):
            note = " — newer messages not included yet"
        header = f"## {day:%a %Y-%m-%d} ({stats.count} messages{note})"
        body = (
            found.summary if found is not None else "Not summarized yet: use search_history or read_messages."
        )
        sections.append(f"{header}\n{body}")
    return "\n\n".join(sections) if sections else "No stored messages in that period."


family = Family("history", toolset, deferred=("chat_info", "inspect_media"))
