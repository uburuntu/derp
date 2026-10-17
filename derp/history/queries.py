"""Read side of chat knowledge: search, exact reads and typed structured queries.

Every function takes the chat id from its caller (the run context), never from
model input. ``ChatQuery`` is the typed request the model fills in; this module
compiles it to SQL itself — no model-written SQL ever runs. Days are the
chat's: ``since``/``until`` without an offset and ``group_by='day'`` follow the
chat's time zone (``Scope.tz``).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, String, and_, case, cast, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.history.models import (
    ChatMember,
    ForumTopic,
    GameScore,
    MemberEvent,
    Message,
    MessageMedia,
    Poll,
    Reaction,
    ReactionCount,
    User,
)
from derp.history.titles import leaderboard

MAX_ROWS = 200
TRIGRAM_THRESHOLD = 0.3

Kind = Literal[
    "text", "photo", "video", "voice", "video_note", "audio", "document", "sticker", "animation",
    "poll", "dice", "location", "venue", "contact", "checklist", "story", "service", "other",
]  # fmt: skip


@dataclass(frozen=True, slots=True)
class Scope:
    """Where a query looks: the chat, and a forum topic unless the whole chat was asked for."""

    chat_id: int
    thread_id: int | None = None
    whole_chat: bool = True
    tz: tzinfo = UTC  # the chat's time zone: day boundaries and offset-less times

    @property
    def zone_name(self) -> str:
        return str(getattr(self.tz, "key", "UTC"))

    def messages(self) -> ColumnElement[bool]:
        clauses = [Message.chat_id == self.chat_id, Message.deleted_at.is_(None)]
        if not self.whole_chat:
            clauses.append(
                Message.thread_id == self.thread_id
                if self.thread_id is not None
                else Message.thread_id.is_(None)
            )
        return and_(*clauses)


async def resolve_members(session: AsyncSession, chat_id: int, names: Sequence[str]) -> list[int]:
    """Match names or @usernames against people seen in this chat (current and past names)."""
    ids: set[int] = set()
    for raw in names:
        name = raw.strip().lstrip("@").lower()
        if not name:
            continue
        pattern = f"%{name}%"
        found = await session.scalars(
            select(ChatMember.user_id)
            .join(User, User.id == ChatMember.user_id)
            .where(ChatMember.chat_id == chat_id)
            .where(
                or_(
                    func.lower(User.username) == name,
                    func.lower(func.concat_ws(" ", User.first_name, User.last_name)).like(pattern),
                    func.lower(cast(ChatMember.names, String)).like(pattern),
                )
            )
        )
        ids.update(found)
    return sorted(ids)


async def search(
    session: AsyncSession,
    scope: Scope,
    query: str,
    *,
    since: datetime | None = None,
    user_ids: Sequence[int] = (),
    limit: int = 20,
) -> list[Message]:
    """Ranked full-text matches (``simple`` config), topped up with fuzzy trigram matches."""
    filters = [scope.messages()]
    if since is not None:
        filters.append(Message.date >= since)
    if user_ids:
        filters.append(Message.user_id.in_(user_ids))
    tsquery = func.websearch_to_tsquery("simple", query)
    age_days = func.extract("epoch", func.now() - Message.date) / 86400
    ranked = await session.scalars(
        select(Message)
        .where(*filters, Message.tsv.op("@@")(tsquery))
        .order_by((func.ts_rank(Message.tsv, tsquery) / (1 + age_days / 30)).desc())
        .limit(limit)
    )
    rows = list(ranked)
    if len(rows) < limit:
        seen = [r.message_id for r in rows] or [0]
        fuzzy = await session.scalars(
            select(Message)
            .where(*filters, Message.message_id.notin_(seen))
            .where(func.word_similarity(query, func.coalesce(Message.text, "")) >= TRIGRAM_THRESHOLD)
            .order_by(
                func.word_similarity(query, func.coalesce(Message.text, "")).desc(), Message.date.desc()
            )
            .limit(limit - len(rows))
        )
        rows.extend(fuzzy)
    return rows


async def around(
    session: AsyncSession, chat_id: int, message_id: int, *, before: int, after: int
) -> list[Message]:
    """Messages around one message, in its own topic, oldest first."""
    anchor = await session.get(Message, (chat_id, message_id))
    scope = Scope(chat_id, anchor.thread_id if anchor else None, whole_chat=anchor is None)
    older = await session.scalars(
        select(Message)
        .where(scope.messages(), Message.message_id < message_id)
        .order_by(Message.message_id.desc())
        .limit(before)
    )
    newer = await session.scalars(
        select(Message)
        .where(scope.messages(), Message.message_id >= message_id)
        .order_by(Message.message_id)
        .limit(after + 1)
    )
    return [*reversed(list(older)), *newer]


async def id_range(
    session: AsyncSession, scope: Scope, first: int, last: int, *, limit: int
) -> list[Message]:
    rows = await session.scalars(
        select(Message)
        .where(scope.messages(), Message.message_id.between(first, last))
        .order_by(Message.message_id)
        .limit(limit)
    )
    return list(rows)


class ChatQuery(BaseModel):
    """A structured question about this chat's stored history."""

    preset: (
        Literal[
            "reaction_leaderboard",
            "quiz_leaderboard",
            "title_leaderboard",
            "top_messages",
            "member_activity",
            "poll_results",
            "joins_leaves",
        ]
        | None
    ) = Field(
        default=None,
        description="reaction_leaderboard: people by reactions received; quiz_leaderboard: points from Derp's "
        "quizzes (days/since/until apply); title_leaderboard: titles of the day won (contains filters the title); "
        "top_messages: most-reacted messages; "
        "member_activity: people by message count; poll_results: polls and their known results; "
        "joins_leaves: membership events. Other fields still filter a preset.",
    )
    kinds: list[Kind] | None = Field(default=None, description="Only these message kinds.")
    users: list[str] | None = Field(
        default=None, description="Only messages from these people (names or @usernames)."
    )
    days: float | None = Field(default=None, gt=0, description="Only the last N days.")
    since: datetime | None = Field(default=None, description="Local time in the chat's time zone.")
    until: datetime | None = Field(default=None, description="Local time in the chat's time zone.")
    whole_chat: bool = Field(default=False, description="Search every forum topic, not only the current one.")
    media_type: (
        Literal["photo", "video", "voice", "video_note", "audio", "document", "sticker", "animation"] | None
    ) = None
    contains: str | None = Field(
        default=None, description="Case-insensitive substring of the text or transcript."
    )
    reaction: str | None = Field(default=None, description="Only messages that got this reaction emoji.")
    min_reactions: int | None = Field(default=None, ge=1)
    from_derp: bool | None = Field(
        default=None, description="True: only Derp's messages; false: exclude them."
    )
    group_by: Literal["user", "day", "kind", "topic", "reaction"] | None = None
    order: Literal["newest", "oldest", "reactions", "count"] = "newest"
    limit: int = Field(default=20, ge=1, le=MAX_ROWS)


@dataclass(frozen=True, slots=True)
class QueryResult:
    kind: Literal["messages", "groups", "polls", "events"]
    messages: list[Message]
    groups: list[tuple[str, int]]
    polls: list[Poll]
    events: list[tuple[MemberEvent, str]]


def _reaction_total() -> Any:
    counted = (
        select(func.sum(ReactionCount.count))
        .where(ReactionCount.chat_id == Message.chat_id, ReactionCount.message_id == Message.message_id)
        .scalar_subquery()
    )
    per_actor = (
        select(func.count())
        .select_from(Reaction)
        .where(Reaction.chat_id == Message.chat_id, Reaction.message_id == Message.message_id)
        .scalar_subquery()
    )
    return func.coalesce(counted, per_actor, literal(0))


def _period(query: ChatQuery, tz: tzinfo) -> tuple[datetime | None, datetime | None]:
    def local(moment: datetime | None) -> datetime | None:
        return moment.replace(tzinfo=tz) if moment is not None and moment.tzinfo is None else moment

    since = local(query.since)
    if query.days is not None:
        since = datetime.now(UTC) - timedelta(days=query.days)
    return since, local(query.until)


def _day(moment: datetime | None, tz: tzinfo) -> Any:
    return moment.astimezone(tz).date() if moment is not None else None


async def _message_filters(
    session: AsyncSession, scope: Scope, query: ChatQuery
) -> list[ColumnElement[bool]] | None:
    filters: list[ColumnElement[bool]] = [scope.messages()]
    since, until = _period(query, scope.tz)
    if since is not None:
        filters.append(Message.date >= since)
    if until is not None:
        filters.append(Message.date <= until)
    if query.kinds:
        filters.append(Message.kind.in_(query.kinds))
    if query.users:
        user_ids = await resolve_members(session, scope.chat_id, query.users)
        if not user_ids:
            return None
        filters.append(Message.user_id.in_(user_ids))
    if query.media_type:
        filters.append(
            select(MessageMedia.message_id)
            .where(
                MessageMedia.chat_id == Message.chat_id,
                MessageMedia.message_id == Message.message_id,
                MessageMedia.type == query.media_type,
            )
            .exists()
        )
    if query.contains:
        pattern = f"%{query.contains.lower()}%"
        filters.append(
            or_(func.lower(Message.text).like(pattern), func.lower(Message.extra_text).like(pattern))
        )
    if query.reaction:
        filters.append(
            or_(
                select(Reaction.actor_id)
                .where(
                    Reaction.chat_id == Message.chat_id,
                    Reaction.message_id == Message.message_id,
                    Reaction.reaction == query.reaction,
                )
                .exists(),
                select(ReactionCount.count)
                .where(
                    ReactionCount.chat_id == Message.chat_id,
                    ReactionCount.message_id == Message.message_id,
                    ReactionCount.reaction == query.reaction,
                )
                .exists(),
            )
        )
    if query.min_reactions:
        filters.append(_reaction_total() >= query.min_reactions)
    if query.from_derp is not None:
        filters.append(Message.is_derp.is_(query.from_derp))
    return filters


async def run_query(session: AsyncSession, scope: Scope, query: ChatQuery) -> QueryResult:
    empty = QueryResult("messages", [], [], [], [])
    match query.preset:
        case "reaction_leaderboard":
            query = query.model_copy(update={"group_by": "user", "order": "reactions", "from_derp": False})
        case "top_messages":
            query = query.model_copy(update={"group_by": None, "order": "reactions", "min_reactions": 1})
        case "member_activity":
            query = query.model_copy(update={"group_by": "user", "order": "count", "from_derp": False})
        case "quiz_leaderboard":
            return QueryResult("groups", [], await _quiz_points(session, scope, query), [], [])
        case "title_leaderboard":
            since, until = _period(query, scope.tz)
            titles = await leaderboard(
                session,
                scope.chat_id,
                contains=query.contains,
                since=_day(since, scope.tz),
                until=_day(until, scope.tz),
                limit=query.limit,
            )
            groups = [(f"{title}: {name}", wins) for title, name, wins in titles]
            return QueryResult("groups", [], groups, [], [])
        case "poll_results":
            return QueryResult("polls", [], [], await _polls(session, scope, query), [])
        case "joins_leaves":
            return QueryResult("events", [], [], [], await _events(session, scope, query))
        case None:
            pass
    filters = await _message_filters(session, scope, query)
    if filters is None:
        return empty
    if query.group_by is None:
        statement = select(Message).where(*filters)
        statement = statement.order_by(
            {
                "newest": Message.date.desc(),
                "oldest": Message.date.asc(),
                "reactions": _reaction_total().desc(),
                "count": Message.date.desc(),
            }[query.order]
        )
        return QueryResult("messages", list(await session.scalars(statement.limit(query.limit))), [], [], [])
    return QueryResult("groups", [], await _groups(session, scope, filters, query), [], [])


async def _groups(
    session: AsyncSession, scope: Scope, filters: list[ColumnElement[bool]], query: ChatQuery
) -> list[tuple[str, int]]:
    if query.group_by == "reaction":
        by_reaction = (
            select(Reaction.reaction, func.count())
            .join(
                Message, and_(Message.chat_id == Reaction.chat_id, Message.message_id == Reaction.message_id)
            )
            .where(*filters)
            .group_by(Reaction.reaction)
            .order_by(func.count().desc())
        )
        return [(str(row[0]), int(row[1])) for row in await session.execute(by_reaction.limit(query.limit))]
    label: Any
    match query.group_by:
        case "user":
            label = func.coalesce(
                func.concat_ws(" ", User.first_name, User.last_name),
                case((Message.is_derp, literal("Derp")), else_=literal("anonymous")),
            )
        case "day":
            local = func.timezone(scope.zone_name, Message.date)
            label = func.to_char(func.date_trunc("day", local), "YYYY-MM-DD")
        case "kind":
            label = Message.kind
        case _:
            label = func.coalesce(
                ForumTopic.name,
                case((Message.thread_id.is_(None), literal("General")), else_=literal("topic")),
            )
    metric = func.sum(_reaction_total()) if query.order == "reactions" else func.count()
    statement = (
        select(label, metric)
        .select_from(Message)
        .outerjoin(User, User.id == Message.user_id)
        .outerjoin(
            ForumTopic, and_(ForumTopic.chat_id == Message.chat_id, ForumTopic.thread_id == Message.thread_id)
        )
        .where(*filters)
        .group_by(label)
    )
    if query.group_by == "day" and query.order in {"newest", "oldest"}:
        statement = statement.order_by(label.desc() if query.order == "newest" else label.asc())
    else:
        statement = statement.order_by(metric.desc())
    return [(str(row[0]), int(row[1] or 0)) for row in await session.execute(statement.limit(query.limit))]


async def _quiz_points(session: AsyncSession, scope: Scope, query: ChatQuery) -> list[tuple[str, int]]:
    since, until = _period(query, scope.tz)
    points = func.sum(GameScore.points)
    statement = (
        select(
            func.coalesce(func.concat_ws(" ", User.first_name, User.last_name), literal("someone")), points
        )
        .select_from(GameScore)
        .outerjoin(User, User.id == GameScore.user_id)
        .where(GameScore.chat_id == scope.chat_id)
        .group_by(GameScore.user_id, User.first_name, User.last_name)
        .having(points > 0)
        .order_by(points.desc())
    )
    if since is not None:
        statement = statement.where(GameScore.day >= _day(since, scope.tz))
    if until is not None:
        statement = statement.where(GameScore.day <= _day(until, scope.tz))
    return [(str(row[0]), int(row[1])) for row in await session.execute(statement.limit(query.limit))]


async def _polls(session: AsyncSession, scope: Scope, query: ChatQuery) -> list[Poll]:
    since, until = _period(query, scope.tz)
    statement = select(Poll).where(Poll.chat_id == scope.chat_id)
    if since is not None:
        statement = statement.where(Poll.created_at >= since)
    if until is not None:
        statement = statement.where(Poll.created_at <= until)
    return list(await session.scalars(statement.order_by(Poll.created_at.desc()).limit(query.limit)))


async def _events(session: AsyncSession, scope: Scope, query: ChatQuery) -> list[tuple[MemberEvent, str]]:
    since, until = _period(query, scope.tz)
    statement = (
        select(MemberEvent, func.concat_ws(" ", User.first_name, User.last_name))
        .outerjoin(User, User.id == MemberEvent.user_id)
        .where(MemberEvent.chat_id == scope.chat_id)
    )
    if since is not None:
        statement = statement.where(MemberEvent.at >= since)
    if until is not None:
        statement = statement.where(MemberEvent.at <= until)
    if query.users:
        statement = statement.where(
            MemberEvent.user_id.in_(await resolve_members(session, scope.chat_id, query.users))
        )
    rows = await session.execute(statement.order_by(MemberEvent.at.desc()).limit(query.limit))
    return [(event, name or "someone") for event, name in rows]
