"""Chat-knowledge tools: search, exact reads, structured queries and the chat profile.

Free on every tier. Scope always comes from the run: the current chat, and the
current forum topic unless the model asks for the whole chat.
"""

from datetime import UTC, datetime, timedelta

from pydantic_ai import RunContext
from pydantic_ai.toolsets import FunctionToolset
from sqlalchemy import func, select

from derp.agent.deps import RunDeps
from derp.agent.toolsets import Family
from derp.history.models import ForumTopic, Message, Poll
from derp.history.queries import ChatQuery, Scope, around, id_range, resolve_members, run_query, search
from derp.history.render import describe_time, load_views, render_transcript

OUTPUT_LIMIT = 8000  # characters returned to the model per call
toolset = FunctionToolset[RunDeps]()


def _scope(ctx: RunContext[RunDeps], whole_chat: bool) -> Scope:
    return Scope(ctx.deps.chat.id, ctx.deps.thread_id, whole_chat=whole_chat or ctx.deps.thread_id is None)


def _clip(text: str) -> str:
    return text if len(text) <= OUTPUT_LIMIT else text[:OUTPUT_LIMIT] + "\n… [more results cut]"


async def _render(ctx: RunContext[RunDeps], rows: list[Message]) -> str:
    async with ctx.deps.db.session() as session:
        views = await load_views(session, sorted(rows, key=lambda r: r.message_id))
    return _clip(render_transcript(views, ctx.deps.tz))


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
            return _clip("\n".join(f"{label}: {value}" for label, value in result.groups))
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


family = Family("history", toolset)
