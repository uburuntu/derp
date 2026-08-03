"""Conversation history loading, grouping, and native message processing."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from aiogram import Bot
from pydantic_ai import ModelMessage, ModelRequest, RunContext, UserPromptPart
from pydantic_core import ValidationError

from derp.catalog import ModelRole
from derp.db import DatabaseManager, get_recent_messages
from derp.history.core import (
    AssistantTextTurn,
    AttachmentReference,
    LogicalTurn,
    Speaker,
    TokenEstimator,
    ToolCall,
    ToolResult,
    ToolRound,
    UserTextTurn,
    materialize_history,
    trim_history,
)
from derp.history.media import (
    DEFAULT_HISTORY_MEDIA_BYTES,
    HydrationCandidate,
    hydrate_media,
    hydration_candidate,
)
from derp.history.persistence import load_source_snapshot
from derp.media import MediaGateway
from derp.models import Message


@dataclass(frozen=True, slots=True)
class HistoryWindow:
    """Product limits for prior logical turns, independent of provider limits."""

    max_turns: int
    max_tokens: int
    query_limit: int
    max_media: int = 8
    max_media_bytes: int = DEFAULT_HISTORY_MEDIA_BYTES

    def __post_init__(self) -> None:
        if (
            min(
                self.max_turns,
                self.max_tokens,
                self.query_limit,
                self.max_media,
                self.max_media_bytes,
            )
            <= 0
        ):
            raise ValueError("History window limits must be positive")
        if self.query_limit < self.max_turns:
            raise ValueError("History query limit cannot be smaller than max turns")


HISTORY_WINDOWS: dict[ModelRole, HistoryWindow] = {
    ModelRole.CHAT_ECONOMY: HistoryWindow(
        max_turns=10,
        max_tokens=24_000,
        query_limit=40,
        max_media=4,
    ),
    ModelRole.CHAT_STANDARD: HistoryWindow(
        max_turns=100,
        max_tokens=128_000,
        query_limit=300,
        max_media=12,
    ),
    ModelRole.CHAT_MULTIMODAL: HistoryWindow(
        max_turns=100,
        max_tokens=128_000,
        query_limit=300,
        max_media=12,
    ),
    ModelRole.CHAT_REASONING: HistoryWindow(
        max_turns=100,
        max_tokens=128_000,
        query_limit=300,
        max_media=12,
    ),
    ModelRole.FREE_TEXT: HistoryWindow(
        max_turns=10,
        max_tokens=24_000,
        query_limit=40,
        max_media=4,
    ),
    ModelRole.FREE_VISUAL: HistoryWindow(
        max_turns=10,
        max_tokens=24_000,
        query_limit=40,
        max_media=4,
    ),
    ModelRole.FREE_AUDIO: HistoryWindow(
        max_turns=10,
        max_tokens=24_000,
        query_limit=40,
        max_media=4,
    ),
}


@dataclass(frozen=True, slots=True)
class LoadedHistory:
    """Native prior messages plus observable window decisions."""

    messages: tuple[ModelMessage, ...]
    turns: tuple[LogicalTurn, ...]
    estimated_tokens: int
    source_messages: int
    hydrated_media: int = 0
    media_failures: int = 0


class ConversationHistoryService:
    """Load one chat/topic scope and materialize prior application history."""

    def __init__(
        self,
        db: DatabaseManager,
        *,
        media_gateway: MediaGateway | None = None,
        bot: Bot | None = None,
    ) -> None:
        self._db = db
        self._media_gateway = media_gateway
        self._bot = bot

    async def load_before(
        self,
        *,
        chat_id: int,
        thread_id: int | None,
        current_date: datetime,
        current_message_id: int,
        window: HistoryWindow,
    ) -> LoadedHistory:
        async with self._db.read_session() as session:
            records = await get_recent_messages(
                session,
                chat_telegram_id=chat_id,
                thread_id=thread_id,
                limit=window.query_limit,
                before_telegram_date=current_date,
                before_telegram_message_id=current_message_id,
            )
        all_turns = logical_turns_from_records(records)
        turns = trim_history(
            all_turns,
            max_turns=window.max_turns,
            max_tokens=window.max_tokens,
        )
        hydrated = {}
        media_failures = 0
        if self._media_gateway is not None and self._bot is not None:
            candidates = _hydration_candidates(records, turns)
            turns = trim_history_for_media(
                turns,
                candidates=candidates,
                max_items=window.max_media,
                max_total_bytes=window.max_media_bytes,
            )
            retained_attachments = _attachment_set(turns)
            candidates = [
                candidate
                for candidate in candidates
                if candidate.history_reference in retained_attachments
            ]
            media = await hydrate_media(
                gateway=self._media_gateway,
                bot=self._bot,
                candidates=candidates,
                max_items=window.max_media,
                max_total_bytes=window.max_media_bytes,
            )
            hydrated = media.content
            media_failures = media.failures
        estimator = TokenEstimator()
        return LoadedHistory(
            messages=materialize_history(
                turns,
                hydrated_attachments=hydrated,
            ),
            turns=turns,
            estimated_tokens=estimator.estimate_history(turns),
            source_messages=len(records),
            hydrated_media=len(hydrated),
            media_failures=media_failures,
        )


def logical_turns_from_records(records: Sequence[Message]) -> tuple[LogicalTurn, ...]:
    """Group flat source rows into atomic ambient or request/response turns."""
    turns: list[LogicalTurn] = []
    root_turn_by_message_id: dict[int, int] = {}
    album_turn_by_media_group_id: dict[str, int] = {}
    tool_rounds_by_turn: dict[int, tuple[ToolRound, ...]] = {}
    for record in records:
        item = _history_item(record)
        if isinstance(item, UserTextTurn):
            record_tool_rounds = (
                _parse_tool_rounds(record.history_dto)
                if record.direction == "in"
                else ()
            )
            if record.media_group_id is not None:
                turn_index = album_turn_by_media_group_id.get(record.media_group_id)
                if turn_index is not None:
                    turn = turns[turn_index]
                    turns[turn_index] = replace(
                        turn,
                        request=_combine_user_fragments(turn.request, item),
                    )
                    root_turn_by_message_id[record.telegram_message_id] = turn_index
                    tool_rounds_by_turn[turn_index] = _merge_tool_rounds(
                        tool_rounds_by_turn.get(turn_index, ()),
                        record_tool_rounds,
                    )
                    continue

            turn_index = len(turns)
            turns.append(LogicalTurn(request=item))
            root_turn_by_message_id[record.telegram_message_id] = turn_index
            if record_tool_rounds:
                tool_rounds_by_turn[turn_index] = record_tool_rounds
            if record.media_group_id is not None:
                album_turn_by_media_group_id[record.media_group_id] = turn_index
            continue

        reply_to_message_id = record.reply_to_message_id
        if reply_to_message_id is None:
            continue
        turn_index = root_turn_by_message_id.get(reply_to_message_id)
        if turn_index is None:
            continue

        turn = turns[turn_index]
        response = turn.response
        if response is None:
            combined = item
        else:
            combined = replace(
                item,
                text=f"{response.text}\n{item.text}",
                attachments=response.attachments + item.attachments,
            )
        turns[turn_index] = replace(
            turn,
            response=combined,
            tool_rounds=_tool_rounds_for_exchange(
                tool_rounds_by_turn.get(turn_index, ()),
                request=turn.request,
                response=combined,
            ),
        )
        root_turn_by_message_id[record.telegram_message_id] = turn_index
    return tuple(turns)


def _parse_tool_rounds(dto: Mapping[str, object]) -> tuple[ToolRound, ...]:
    raw_rounds = dto.get("tool_rounds")
    if not isinstance(raw_rounds, list):
        return ()

    parsed: list[ToolRound] = []
    for raw_round in raw_rounds:
        try:
            parsed.append(_parse_tool_round(raw_round))
        except KeyError, TypeError, ValueError:
            continue
    return _merge_tool_rounds((), tuple(parsed))


def _parse_tool_round(raw: object) -> ToolRound:
    if not isinstance(raw, Mapping):
        raise TypeError("Tool round must be an object")
    raw_calls = raw.get("calls")
    raw_results = raw.get("results")
    if not isinstance(raw_calls, list) or not isinstance(raw_results, list):
        raise TypeError("Tool round calls and results must be arrays")

    calls = tuple(_parse_tool_call(value) for value in raw_calls)
    results = tuple(_parse_tool_result(value) for value in raw_results)
    results_by_id = {result.tool_call_id: result for result in results}
    if len(results_by_id) != len(results):
        raise ValueError("Tool result IDs must be unique")
    ordered_results = tuple(results_by_id[call.tool_call_id] for call in calls)
    return ToolRound(
        calls=calls,
        results=ordered_results,
        called_at=_parse_tool_timestamp(raw.get("called_at")),
        returned_at=_parse_tool_timestamp(raw.get("returned_at")),
    )


def _parse_tool_call(raw: object) -> ToolCall:
    if not isinstance(raw, Mapping):
        raise TypeError("Tool call must be an object")
    return ToolCall(
        tool_name=_required_string(raw, "tool_name"),
        tool_call_id=_required_string(raw, "tool_call_id"),
        arguments_json=_required_string(raw, "arguments_json"),
    )


def _parse_tool_result(raw: object) -> ToolResult:
    if not isinstance(raw, Mapping):
        raise TypeError("Tool result must be an object")
    return ToolResult(
        tool_name=_required_string(raw, "tool_name"),
        tool_call_id=_required_string(raw, "tool_call_id"),
        content=_required_string(raw, "content"),
    )


def _required_string(value: Mapping[object, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return item


def _parse_tool_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("Tool timestamps must be strings")
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Tool timestamps must be timezone-aware")
    return timestamp


def _merge_tool_rounds(
    current: Sequence[ToolRound],
    additional: Sequence[ToolRound],
) -> tuple[ToolRound, ...]:
    retained = list(current)
    seen_call_ids = {call.tool_call_id for round_ in retained for call in round_.calls}
    last_returned_at = retained[-1].returned_at if retained else None
    for round_ in additional:
        call_ids = {call.tool_call_id for call in round_.calls}
        if call_ids & seen_call_ids:
            continue
        if last_returned_at is not None and round_.called_at < last_returned_at:
            continue
        retained.append(round_)
        seen_call_ids.update(call_ids)
        last_returned_at = round_.returned_at
    return tuple(retained)


def _tool_rounds_for_exchange(
    rounds: Sequence[ToolRound],
    *,
    request: UserTextTurn,
    response: AssistantTextTurn,
) -> tuple[ToolRound, ...]:
    return tuple(
        round_
        for round_ in rounds
        if request.timestamp <= round_.called_at
        and round_.returned_at <= response.timestamp
    )


def trim_history_for_media(
    turns: Sequence[LogicalTurn],
    *,
    candidates: Sequence[HydrationCandidate],
    max_items: int,
    max_total_bytes: int,
) -> tuple[LogicalTurn, ...]:
    """Keep newest complete turns whose supported media fit both limits.

    Unknown-size media reserves the complete byte budget. A single declared
    oversize item stays in its turn as an unavailable marker and is not
    scheduled. The normalized snapshot is untrusted input, so hydration never
    schedules an unbounded aggregate download.
    """
    if max_items < 0 or max_total_bytes < 0:
        raise ValueError("Media history limits must not be negative")

    by_reference = {candidate.history_reference: candidate for candidate in candidates}
    retained_newest_first: list[LogicalTurn] = []
    retained_items = 0
    retained_bytes = 0
    for turn in reversed(turns):
        turn_reservations: list[int] = []
        for reference in _turn_attachments(turn):
            candidate = by_reference.get(reference)
            if candidate is None:
                continue
            reservation = _media_reservation(
                candidate,
                max_total_bytes=max_total_bytes,
            )
            if reservation is not None:
                turn_reservations.append(reservation)
        turn_items = len(turn_reservations)
        turn_bytes = sum(turn_reservations)
        if (
            retained_items + turn_items > max_items
            or retained_bytes + turn_bytes > max_total_bytes
        ):
            break
        retained_newest_first.append(turn)
        retained_items += turn_items
        retained_bytes += turn_bytes
    return tuple(reversed(retained_newest_first))


def _media_reservation(
    candidate: HydrationCandidate,
    *,
    max_total_bytes: int,
) -> int | None:
    declared_bytes = candidate.media_reference.metadata.file_size
    if declared_bytes is not None and declared_bytes > max_total_bytes:
        return None
    return declared_bytes or max_total_bytes


def _combine_user_fragments(
    first: UserTextTurn,
    current: UserTextTurn,
) -> UserTextTurn:
    return replace(
        first,
        text=f"{first.text}\n{current.text}",
        attachments=first.attachments + current.attachments,
    )


def process_native_history(
    ctx: RunContext[Any],
    messages: list[ModelMessage],
) -> list[ModelMessage]:
    """Keep the current request plus a bounded number of prior logical turns.

    ``ProcessHistory`` runs after Pydantic AI appends the current request to the
    message history. The final chunk is therefore the in-progress turn and is
    retained independently from the product limit on prior turns.
    """
    window = getattr(ctx.deps, "history_window", None)
    if not isinstance(window, HistoryWindow):
        return messages
    chunks = _native_turn_chunks(messages)
    if not chunks:
        return messages

    current = chunks[-1]
    retained: list[list[ModelMessage]] = [current]
    estimator = TokenEstimator()
    estimated = _estimate_native_chunk(current, estimator)
    prior_turns = 0
    for chunk in reversed(chunks[:-1]):
        if prior_turns == window.max_turns:
            break
        chunk_tokens = _estimate_native_chunk(chunk, estimator)
        if estimated + chunk_tokens > window.max_tokens:
            break
        retained.append(chunk)
        estimated += chunk_tokens
        prior_turns += 1
    return [message for chunk in reversed(retained) for message in chunk]


def _native_turn_chunks(messages: Sequence[ModelMessage]) -> list[list[ModelMessage]]:
    chunks: list[list[ModelMessage]] = []
    for message in messages:
        starts_turn = isinstance(message, ModelRequest) and any(
            isinstance(part, UserPromptPart) for part in message.parts
        )
        if starts_turn or not chunks:
            chunks.append([message])
        else:
            chunks[-1].append(message)
    return chunks


def _estimate_native_chunk(
    messages: Sequence[ModelMessage], estimator: TokenEstimator
) -> int:
    return sum(estimator.estimate_text(repr(message)) + 4 for message in messages)


def _history_item(record: Message) -> UserTextTurn | AssistantTextTurn:
    dto = record.history_dto
    role = str(dto.get("kind", "")).removesuffix("_text") or record.role
    timestamp = _parse_timestamp(dto.get("timestamp"), record.telegram_date)
    speaker_data = dto.get("speaker")
    if isinstance(speaker_data, dict):
        speaker = Speaker(
            id=_optional_int(speaker_data.get("id")),
            display_name=str(speaker_data.get("display_name") or "Unknown sender"),
        )
    elif record.user:
        speaker = Speaker(
            id=record.user.telegram_id,
            display_name=record.user.display_name,
        )
    else:
        speaker = Speaker(id=None, display_name="Unknown sender")
    text = str(
        dto.get("text") or record.text or f"[{record.content_type or 'message'}]"
    )
    attachments = _attachment_references(dto)
    values = {
        "source_message_id": record.telegram_message_id,
        "timestamp": timestamp,
        "speaker": speaker,
        "text": text,
        "attachments": attachments,
    }
    if role == "assistant" or record.direction == "out":
        return AssistantTextTurn(**values)
    return UserTextTurn(**values)


def _attachment_references(dto: dict[str, object]) -> tuple[AttachmentReference, ...]:
    raw = dto.get("attachments")
    if not isinstance(raw, list):
        return ()
    references: list[AttachmentReference] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            references.append(
                AttachmentReference(
                    media_type=str(item["media_type"]),
                    file_id=str(item["file_id"]),
                    file_unique_id=str(item["file_unique_id"]),
                )
            )
        except KeyError, ValueError:
            continue
    return tuple(references)


def _parse_timestamp(value: object, fallback: datetime) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return fallback


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _hydration_candidates(
    records: Sequence[Message],
    turns: Sequence[LogicalTurn],
) -> list[HydrationCandidate]:
    needed = {
        attachment
        for turn in turns
        for attachment in (
            turn.request.attachments
            + (turn.response.attachments if turn.response else ())
        )
    }
    candidates: list[HydrationCandidate] = []
    seen: set[AttachmentReference] = set()
    for record in records:
        try:
            snapshot = load_source_snapshot(record.source_snapshot)
        except ValidationError, TypeError, ValueError:
            continue
        for attachment in snapshot.attachments:
            candidate = hydration_candidate(attachment)
            if (
                candidate is not None
                and candidate.history_reference in needed
                and candidate.history_reference not in seen
            ):
                candidates.append(candidate)
                seen.add(candidate.history_reference)
    return candidates


def _turn_attachments(turn: LogicalTurn) -> tuple[AttachmentReference, ...]:
    response_attachments = turn.response.attachments if turn.response else ()
    return turn.request.attachments + response_attachments


def _attachment_set(
    turns: Sequence[LogicalTurn],
) -> set[AttachmentReference]:
    return {attachment for turn in turns for attachment in _turn_attachments(turn)}


__all__ = [
    "HISTORY_WINDOWS",
    "ConversationHistoryService",
    "HistoryWindow",
    "LoadedHistory",
    "logical_turns_from_records",
    "process_native_history",
    "trim_history_for_media",
]
