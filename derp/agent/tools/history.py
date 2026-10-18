"""Chat-knowledge tools: search, exact reads, structured queries, day digests and the chat profile.

Free on every tier, except that building a digest is a model call charged to the
run. Scope always comes from the run: the current chat, and the current forum
topic unless the model asks for the whole chat.
"""

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Literal, get_args

from pydantic_ai import BinaryContent, RunContext
from pydantic_ai.toolsets import FunctionToolset
from sqlalchemy import func, select

from derp.agent.deps import RunDeps
from derp.agent.digests import brief, build
from derp.agent.media import CLIPS, IMAGE_LIKE, as_content, inspect, note_images, referenced_media
from derp.agent.toolsets import Family
from derp.history.digests import (
    aimed_at,
    day_bounds,
    day_stats,
    is_current,
    last_said,
    scope_key,
    span_messages,
)
from derp.history.models import Digest, ForumTopic, Message, Poll
from derp.history.queries import (
    ChatQuery,
    Scope,
    around,
    conversations,
    id_range,
    resolve_members,
    run_query,
    search,
)
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


SEARCH_HITS = 8  # best matches shown, each with the messages around it
SEARCH_CONTEXT = 2  # messages shown before and after each match


@toolset.tool
async def search_history(
    ctx: RunContext[RunDeps],
    queries: list[str],
    days: float | None = None,
    user: str | None = None,
    whole_chat: bool = False,
) -> str:
    """Find stored messages in this chat by gist (text, voice transcripts, media descriptions), with context.

    People remember the gist, not the wording: send up to five short phrasings in one call — the asker's
    words, synonyms and paraphrases, the Russian and English versions, names as written in chat
    (nicknames, @usernames). Any word may match; messages matching more phrasings rank higher.
    Each match comes with the messages around it.

    Args:
        queries: Keywords or short phrases, one phrasing each, best guess first.
        days: Only the last N days.
        user: Only messages from this person (name or @username).
        whole_chat: Search all forum topics instead of the current one.
    """
    since = datetime.now(UTC) - timedelta(days=days) if days else None
    async with ctx.deps.db.session() as session:
        user_ids = await resolve_members(session, ctx.deps.chat.id, [user]) if user else []
        if user and not user_ids:
            return f"No one called {user!r} has been seen in this chat."
        hits = await search(
            session, _scope(ctx, whole_chat), queries[:5], since=since, user_ids=user_ids, limit=SEARCH_HITS
        )
        windows = await conversations(session, hits, before=SEARCH_CONTEXT, after=SEARCH_CONTEXT)
        blocks = [render_transcript(await load_views(session, window), ctx.deps.tz) for window in windows]
    if not blocks:
        return "No matching messages are stored (older messages may have expired)."
    return "\n\n".join(blocks)


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
        f"#{item.row.message_id}{f' ({item.still})' if item.still else ''}: {item.row.description or item.row.type}"
        for item in inspected
    ]
    if not deps.pin.vision:
        return "\n".join(notes)
    return [*notes, *as_content(inspected)]


PAID_BUILDS = 7  # summaries a paid run may make per call; other runs make one
CATCH_UP_DAYS = 7  # how far back a catch-up reaches
RAW_SPAN_CHARS = 3000  # a fresh stretch this short is shown as is rather than summarized
ASKER_LINES = 10


async def _affordable(deps: RunDeps) -> bool:
    """Side calls ledgered so far still fit under the run's hold."""
    if deps.funds.reservation_id is None:
        return True
    async with deps.db.session() as session:
        spent = await run_cost(session, deps.run_id)
    return deps.settings.credits_for_usd(float(spent)) < deps.funds.credits


async def _anchor(deps: RunDeps, scope: Scope, period: str, message_id: int | None) -> Message | str:
    """The message a catch-up starts after, or why there is none."""
    async with deps.db.session() as session:
        if period == "since_my_last_message":
            found = await last_said(session, scope, deps.user_id, deps.trigger_message_id)
            return found or "None of the asker's earlier messages here are stored: pick a period instead."
        if message_id is None:
            trigger = await session.get(Message, (deps.chat.id, deps.trigger_message_id))
            message_id = trigger.reply_to_message_id if trigger else None
        if message_id is None:
            return "Give message_id, or the asker can reply to the message to catch up from."
        found = await session.get(Message, (deps.chat.id, message_id))
    if found is None or found.deleted_at is not None or found.message_id >= deps.trigger_message_id:
        return f"Message #{message_id} isn't stored here (or comes after the question)."
    return found


async def _day(
    deps: RunDeps, scope: Scope, day: date, may_build: bool, *, alone: bool
) -> tuple[str | None, bool]:
    """A day's digest section (cached, or built when allowed) and whether a summary was made."""
    key = scope_key(scope)
    async with deps.db.session() as session:
        stats = await day_stats(session, scope, day)
        cached = await session.get(Digest, (deps.chat.id, key, day)) if stats else None
    if stats is None:
        return (f"{day:%a %Y-%m-%d}: no stored messages." if alone else None), False
    found, note, built = cached, "", False
    if not is_current(cached, stats) and may_build and await _affordable(deps):
        built = True
        try:
            found = await build(deps, scope, key, day)
        except Exception as error:  # noqa: BLE001 - fall back to the cached digest or a pointer
            log.info("digest build failed: %s", type(error).__name__)
    if found is not None and found.stale:  # it may quote what was edited or deleted since
        found = None
    elif found is not None and not is_current(found, stats):
        note = " — newer messages not included yet"
    header = f"## {day:%a %Y-%m-%d} ({stats.count} messages{note})"
    body = found.summary if found is not None else "Not summarized yet: use search_history or read_messages."
    return f"{header}\n{body}", built


async def _rest_of_day(deps: RunDeps, scope: Scope, anchor: Message, may_build: bool) -> str:
    """What followed ``anchor`` on its own day, fresh: as is when short, else summarized when allowed."""
    until = day_bounds(anchor.date.astimezone(scope.tz).date(), scope.tz)[1]
    async with deps.db.session() as session:
        rows = await span_messages(session, scope, anchor.message_id, deps.trigger_message_id, until)
        transcript = render_transcript(await load_views(session, rows), scope.tz)
    header = f"## After #{anchor.message_id} ({describe_time(anchor.date, scope.tz)}): {len(rows)} messages"
    if not rows or len(transcript) <= RAW_SPAN_CHARS:
        return f"{header}\n{transcript}".strip()
    pointer = f"Not summarized: read_messages first_id={rows[0].message_id} last_id={rows[-1].message_id}."
    if not may_build or not await _affordable(deps):
        return f"{header}\n{pointer}"
    try:
        text, _served = await brief(deps, scope.chat_id, transcript)
    except Exception as error:  # noqa: BLE001 - fall back to a pointer
        log.info("catch-up summary failed: %s", type(error).__name__)
        text = pointer
    return f"{header}\n{text}"


async def _for_asker(
    deps: RunDeps, scope: Scope, start: datetime, end: datetime | None, after_id: int
) -> str | None:
    async with deps.db.session() as session:
        rows = await aimed_at(
            session, scope, deps.user_id, start=start, end=end,
            after_id=after_id, before_id=deps.trigger_message_id, limit=ASKER_LINES,
        )  # fmt: skip
        if not rows:
            return None
        lines = render_transcript(await load_views(session, rows), deps.tz)
    return f"## For the asker: replies to them and mentions of them\n{lines}"


CatchUp = Literal["since_my_last_message", "since_message"]


@toolset.tool
async def digest(
    ctx: RunContext[RunDeps],
    period: Literal["today", "yesterday", "last_7_days"] | CatchUp | date = "today",
    message_id: int | None = None,
    whole_chat: bool = False,
) -> str:
    """Catch someone up: what was discussed, decided and planned, from summaries of chat days (chat's time zone).

    Prefer this over reading transcripts for "what happened / what did I miss" questions; cite its #ids.
    Lead with what concerns the asker (questions waiting for them first), then the rest, briefly.

    Args:
        period: today, yesterday, last_7_days, one date (YYYY-MM-DD), since_my_last_message (everything after
            the asker's last message here), or since_message (everything after message_id).
        message_id: For since_message: the #id to start after; by default the message the asker replied to.
        whole_chat: Summarize all forum topics instead of the current one.
    """
    deps = ctx.deps
    scope = _scope(ctx, whole_chat)
    today = datetime.now(deps.tz).date()
    catch_up = period in get_args(CatchUp)
    anchor: Message | None = None
    match period:
        case "today":
            days = [today]
        case "yesterday":
            days = [today - timedelta(days=1)]
        case "last_7_days":
            days = [today - timedelta(days=n) for n in range(CATCH_UP_DAYS - 1, -1, -1)]
        case "since_my_last_message" | "since_message":
            found = await _anchor(deps, scope, period, message_id)
            if isinstance(found, str):
                return found
            first = found.date.astimezone(deps.tz).date()
            if (today - first).days < CATCH_UP_DAYS:  # the rest of its day fresh, then whole days
                anchor, first = found, first + timedelta(days=1)
            else:  # too far back: the last days, whole
                first = today - timedelta(days=CATCH_UP_DAYS - 1)
            days = [first + timedelta(days=n) for n in range((today - first).days + 1)]
        case _:
            if period > today:
                return "That day hasn't happened yet."
            days = [period]
    builds = PAID_BUILDS if deps.funding is Funding.PAID else 1
    sections: list[str] = []
    for day in reversed(days):  # the newest first: they get the summaries this call may make
        section, built = await _day(deps, scope, day, builds > 0, alone=not catch_up and len(days) == 1)
        builds -= built
        if section:
            sections.append(section)
    if anchor is not None:
        sections.append(await _rest_of_day(deps, scope, anchor, builds > 0))
    sections.reverse()
    start = anchor.date if anchor is not None else day_bounds(days[0], deps.tz)[0]
    end = None if catch_up else day_bounds(days[-1], deps.tz)[1]
    after_id = anchor.message_id if anchor is not None else 0
    if aimed := await _for_asker(deps, scope, start, end, after_id):
        sections.insert(0, aimed)
    return "\n\n".join(sections) if sections else "No stored messages in that period."


family = Family("history", toolset, deferred=("chat_info", "inspect_media"))
