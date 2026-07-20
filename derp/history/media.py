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
    """Hydrate newest references within item, concurrency, and aggregate limits."""
    if max_items <= 0 or concurrency <= 0:
        raise ValueError("Media hydration limits must be positive")
    if max_total_bytes < 0:
        raise ValueError("Aggregate media byte limit must not be negative")
    selected = candidates[-max_items:]

    # Reserve declared bytes newest-first. Unknown or zero sizes reserve the
    # remaining budget so untrusted metadata cannot schedule an unbounded batch.
    planned: list[HydrationCandidate] = []
    reserved_bytes = 0
    for candidate in reversed(selected):
        remaining = max_total_bytes - reserved_bytes
        if remaining <= 0:
            break
        declared_bytes = candidate.media_reference.metadata.file_size
        reservation = (
            declared_bytes if declared_bytes and declared_bytes > 0 else remaining
        )
        if reservation > remaining:
            continue
        planned.append(candidate)
        reserved_bytes += reservation

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
                continue
            content_by_reference[candidate.history_reference] = BinaryContent(
                data=data,
                media_type=candidate.media_reference.metadata.mime_type,
            )
            retained_bytes += len(data)

    # Restore source order after newest-first budget selection.
    content = {
        candidate.history_reference: content_by_reference[candidate.history_reference]
        for candidate in selected
        if candidate.history_reference in content_by_reference
    }
    return HydratedMedia(
        content=content,
        failures=len(selected) - len(content),
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
