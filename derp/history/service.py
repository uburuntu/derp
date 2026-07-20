"""Conversation history loading, grouping, and native message processing."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from aiogram import Bot
from pydantic_ai import ModelMessage, ModelRequest, RunContext, UserPromptPart
from pydantic_core import ValidationError

from derp.catalog import GoogleModelKey
from derp.db import DatabaseManager, get_recent_messages
from derp.history.core import (
    AssistantTextTurn,
    AttachmentReference,
    LogicalTurn,
    Speaker,
    TokenEstimator,
    UserTextTurn,
    materialize_history,
    trim_history,
)
from derp.history.media import (
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

    def __post_init__(self) -> None:
        if min(self.max_turns, self.max_tokens, self.query_limit, self.max_media) <= 0:
            raise ValueError("History window limits must be positive")
        if self.query_limit < self.max_turns:
            raise ValueError("History query limit cannot be smaller than max turns")


HISTORY_WINDOWS: dict[GoogleModelKey, HistoryWindow] = {
    GoogleModelKey.CHAT_ECONOMY: HistoryWindow(
        max_turns=10,
        max_tokens=24_000,
        query_limit=40,
        max_media=4,
    ),
    GoogleModelKey.CHAT_STANDARD: HistoryWindow(
        max_turns=100,
        max_tokens=128_000,
        query_limit=300,
        max_media=12,
    ),
    GoogleModelKey.CHAT_REASONING: HistoryWindow(
        max_turns=100,
        max_tokens=128_000,
        query_limit=300,
        max_media=12,
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
            media = await hydrate_media(
                gateway=self._media_gateway,
                bot=self._bot,
                candidates=candidates,
                max_items=window.max_media,
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
    for record in records:
        item = _history_item(record)
        if isinstance(item, UserTextTurn):
            if record.media_group_id is not None:
                turn_index = album_turn_by_media_group_id.get(record.media_group_id)
                if turn_index is not None:
                    turn = turns[turn_index]
                    turns[turn_index] = replace(
                        turn,
                        request=_combine_user_fragments(turn.request, item),
                    )
                    root_turn_by_message_id[record.telegram_message_id] = turn_index
                    continue

            turn_index = len(turns)
            turns.append(LogicalTurn(request=item))
            root_turn_by_message_id[record.telegram_message_id] = turn_index
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
        turns[turn_index] = replace(turn, response=combined)
        root_turn_by_message_id[record.telegram_message_id] = turn_index
    return tuple(turns)


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


__all__ = [
    "HISTORY_WINDOWS",
    "ConversationHistoryService",
    "HistoryWindow",
    "LoadedHistory",
    "logical_turns_from_records",
    "process_native_history",
]
