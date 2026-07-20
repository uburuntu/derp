"""Versioned persistence projections for normalized Telegram history."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from pydantic import TypeAdapter

from derp.history.core import AttachmentReference, Speaker
from derp.history.snapshot import TelegramMessageSnapshot

HISTORY_DTO_SCHEMA_VERSION: Final = 1
CANONICAL_PROJECTION_SCHEMA_VERSION: Final = 1

_SNAPSHOT_ADAPTER = TypeAdapter(TelegramMessageSnapshot)


@dataclass(frozen=True, slots=True)
class PersistedMessageProjection:
    """The three durable representations derived from one source message."""

    source_snapshot: dict[str, object]
    history_dto: dict[str, object]
    canonical_projection: dict[str, object]
    text: str


def project_persisted_message(
    snapshot: TelegramMessageSnapshot,
) -> PersistedMessageProjection:
    """Derive versioned source, application, and canonical representations."""
    source = _SNAPSHOT_ADAPTER.dump_python(snapshot, mode="json")
    if not isinstance(source, dict):  # pragma: no cover - adapter contract
        raise TypeError("Telegram snapshot must serialize to an object")

    text = snapshot.text or snapshot.caption or _attachment_marker(snapshot)
    speaker = _speaker(snapshot)
    attachments = [
        {
            "media_type": attachment.media_type.value,
            "file_id": attachment.file_id,
            "file_unique_id": attachment.file_unique_id,
        }
        for attachment in snapshot.attachments
    ]
    history_dto: dict[str, object] = {
        "schema_version": HISTORY_DTO_SCHEMA_VERSION,
        "kind": f"{snapshot.role.value}_text",
        "source_message_id": snapshot.message_id,
        "timestamp": str(source["sent_at"]),
        "speaker": {"id": speaker.id, "display_name": speaker.display_name},
        "text": text,
        "attachments": attachments,
    }
    canonical_projection: dict[str, object] = {
        "schema_version": CANONICAL_PROJECTION_SCHEMA_VERSION,
        "role": snapshot.role.value,
        "text": text,
        "attachment_types": [
            attachment.media_type.value for attachment in snapshot.attachments
        ],
        "reply_to_message_id": snapshot.reply.message_id,
    }
    return PersistedMessageProjection(
        source_snapshot=source,
        history_dto=history_dto,
        canonical_projection=canonical_projection,
        text=text,
    )


def load_source_snapshot(data: object) -> TelegramMessageSnapshot:
    """Validate a stored source snapshot at the application boundary."""
    return _SNAPSHOT_ADAPTER.validate_python(data)


def attachment_references(
    projection: PersistedMessageProjection,
) -> tuple[AttachmentReference, ...]:
    """Read stable attachment references from a validated application DTO."""
    raw = projection.history_dto.get("attachments")
    if not isinstance(raw, list):
        raise ValueError("History DTO attachments must be a list")
    references: list[AttachmentReference] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("History DTO attachment must be an object")
        references.append(
            AttachmentReference(
                media_type=str(item["media_type"]),
                file_id=str(item["file_id"]),
                file_unique_id=str(item["file_unique_id"]),
            )
        )
    return tuple(references)


def _speaker(snapshot: TelegramMessageSnapshot) -> Speaker:
    sender = snapshot.sender
    if sender is None:
        return Speaker(id=None, display_name="Unknown sender")
    if sender.title:
        display_name = sender.title
    elif sender.username:
        display_name = f"@{sender.username}"
    else:
        display_name = " ".join(
            part for part in (sender.first_name, sender.last_name) if part
        ) or str(sender.id)
    return Speaker(id=sender.id, display_name=display_name)


def _attachment_marker(snapshot: TelegramMessageSnapshot) -> str:
    if not snapshot.attachments:
        return f"[{snapshot.content_type}]"
    types = ", ".join(
        attachment.media_type.value for attachment in snapshot.attachments
    )
    return f"[{types}]"


__all__ = [
    "CANONICAL_PROJECTION_SCHEMA_VERSION",
    "HISTORY_DTO_SCHEMA_VERSION",
    "PersistedMessageProjection",
    "attachment_references",
    "load_source_snapshot",
    "project_persisted_message",
]
