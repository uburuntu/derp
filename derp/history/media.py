"""Convert durable history attachments into bounded ephemeral model content."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

import logfire
from aiogram import Bot
from pydantic_ai import BinaryContent

from derp.history.core import AttachmentReference
from derp.history.snapshot import AttachmentMediaType, AttachmentSnapshot
from derp.media import (
    MediaFamily,
    MediaGateway,
    MediaGatewayError,
    MediaMetadata,
    MediaReference,
)

DEFAULT_AGGREGATE_MEDIA_BYTES: Final = 32 * 1024 * 1024
DEFAULT_HISTORY_MEDIA_BYTES: Final = 12 * 1024 * 1024

_DEFAULT_MIME = MappingProxyType(
    {
        AttachmentMediaType.ANIMATION: "video/mp4",
        AttachmentMediaType.AUDIO: "audio/mpeg",
        AttachmentMediaType.DOCUMENT: "application/pdf",
        AttachmentMediaType.LIVE_PHOTO: "video/mp4",
        AttachmentMediaType.PHOTO: "image/jpeg",
        AttachmentMediaType.STICKER: "image/webp",
        AttachmentMediaType.VIDEO: "video/mp4",
        AttachmentMediaType.VIDEO_NOTE: "video/mp4",
        AttachmentMediaType.VOICE: "audio/ogg",
    }
)

_FAMILY = MappingProxyType(
    {
        AttachmentMediaType.ANIMATION: MediaFamily.VIDEO,
        AttachmentMediaType.AUDIO: MediaFamily.AUDIO,
        AttachmentMediaType.DOCUMENT: MediaFamily.DOCUMENT,
        AttachmentMediaType.LIVE_PHOTO: MediaFamily.VIDEO,
        AttachmentMediaType.PHOTO: MediaFamily.IMAGE,
        AttachmentMediaType.STICKER: MediaFamily.IMAGE,
        AttachmentMediaType.VIDEO: MediaFamily.VIDEO,
        AttachmentMediaType.VIDEO_NOTE: MediaFamily.VIDEO,
        AttachmentMediaType.VOICE: MediaFamily.AUDIO,
    }
)


@dataclass(frozen=True, slots=True)
class HydrationCandidate:
    """One application reference paired with transport metadata."""

    history_reference: AttachmentReference
    media_reference: MediaReference
    family: MediaFamily


@dataclass(frozen=True, slots=True)
class HydratedMedia:
    """Successful ephemeral bytes and the count of graceful degradations."""

    content: dict[AttachmentReference, BinaryContent]
    failures: int
    downloaded_bytes: int = 0


def hydration_candidate(attachment: AttachmentSnapshot) -> HydrationCandidate | None:
    """Build a validated transport candidate or reject unsupported documents."""
    mime_type = attachment.mime_type or _DEFAULT_MIME[attachment.media_type]
    family = _FAMILY[attachment.media_type]
    if family is MediaFamily.DOCUMENT and mime_type != "application/pdf":
        return None
    history_reference = AttachmentReference(
        media_type=attachment.media_type.value,
        file_id=attachment.file_id,
        file_unique_id=attachment.file_unique_id,
    )
    return HydrationCandidate(
        history_reference=history_reference,
        media_reference=MediaReference(
            file_id=attachment.file_id,
            file_unique_id=attachment.file_unique_id,
            metadata=MediaMetadata(
                mime_type=mime_type,
                file_size=attachment.size,
                file_name=attachment.filename,
                width=attachment.width,
                height=attachment.height,
                duration_seconds=attachment.duration,
            ),
        ),
        family=family,
    )


async def hydrate_media(
    *,
    gateway: MediaGateway,
    bot: Bot,
    candidates: list[HydrationCandidate],
    max_items: int,
    max_total_bytes: int = DEFAULT_HISTORY_MEDIA_BYTES,
    concurrency: int = 3,
) -> HydratedMedia:
    """Hydrate a caller-trimmed set of references within an aggregate byte cap.

    Logical-turn selection belongs to the history service. Rejecting an
    overlarge item set here prevents this transport helper from silently
    dropping individual attachments out of an otherwise retained turn.
    """
    if max_items <= 0 or concurrency <= 0:
        raise ValueError("Media hydration limits must be positive")
    if max_total_bytes < 0:
        raise ValueError("Aggregate media byte limit must not be negative")
    declared_oversize = [
        candidate
        for candidate in candidates
        if (
            candidate.media_reference.metadata.file_size is not None
            and candidate.media_reference.metadata.file_size > max_total_bytes
        )
    ]
    planned = [
        candidate
        for candidate in reversed(candidates)
        if candidate not in declared_oversize
    ]
    if len(planned) > max_items:
        raise ValueError(
            "Media candidates must be trimmed by complete logical turn before hydration"
        )
    reserved_bytes = sum(
        candidate.media_reference.metadata.file_size or max_total_bytes
        for candidate in planned
    )
    if reserved_bytes > max_total_bytes:
        raise ValueError(
            "Media candidates must be trimmed by complete logical turn before hydration"
        )

    # Download newest-first so inaccurate Telegram size metadata degrades the
    # oldest candidate first while the actual retained byte total remains safe.
    for candidate in declared_oversize:
        logfire.warning(
            "history_media_unavailable",
            media_type=candidate.history_reference.media_type,
            error_type="AggregateMediaTooLarge",
        )

    async def hydrate_one(
        candidate: HydrationCandidate,
    ) -> tuple[HydrationCandidate, bytes] | None:
        try:
            data = await gateway.download(
                bot=bot,
                reference=candidate.media_reference,
                expected_family=candidate.family,
            )
            return candidate, data
        except MediaGatewayError as exc:
            logfire.warning(
                "history_media_unavailable",
                media_type=candidate.history_reference.media_type,
                error_type=type(exc).__name__,
            )
            return None

    # Process small batches so completed downloads do not accumulate while later
    # candidates are still in flight. Actual bytes are checked even when the
    # Telegram-declared size was inaccurate.
    content_by_reference: dict[AttachmentReference, BinaryContent] = {}
    retained_bytes = 0
    for offset in range(0, len(planned), concurrency):
        batch = planned[offset : offset + concurrency]
        results = await asyncio.gather(*(hydrate_one(item) for item in batch))
        for result in results:
            if result is None:
                continue
            candidate, data = result
            if retained_bytes + len(data) > max_total_bytes:
                logfire.warning(
                    "history_media_unavailable",
                    media_type=candidate.history_reference.media_type,
                    error_type="AggregateMediaTooLarge",
                )
                continue
            content_by_reference[candidate.history_reference] = BinaryContent(
                data=data,
                media_type=candidate.media_reference.metadata.mime_type,
            )
            retained_bytes += len(data)

    # Restore source order after newest-first hydration.
    content = {
        candidate.history_reference: content_by_reference[candidate.history_reference]
        for candidate in candidates
        if candidate.history_reference in content_by_reference
    }
    return HydratedMedia(
        content=content,
        failures=len(candidates) - len(content),
        downloaded_bytes=retained_bytes,
    )


__all__ = [
    "DEFAULT_AGGREGATE_MEDIA_BYTES",
    "DEFAULT_HISTORY_MEDIA_BYTES",
    "HydratedMedia",
    "HydrationCandidate",
    "hydrate_media",
    "hydration_candidate",
]
