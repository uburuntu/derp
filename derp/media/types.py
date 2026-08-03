"""Provider-neutral media references and validation vocabulary."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_MIME_TYPE_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$"
)


def normalize_mime_type(value: str) -> str:
    """Return a canonical bare MIME type or reject malformed input."""
    mime_type = value.partition(";")[0].strip().lower()
    if not _MIME_TYPE_PATTERN.fullmatch(mime_type):
        raise ValueError("mime_type must be a valid type/subtype value")
    return mime_type


class MediaFamily(StrEnum):
    """Media categories with distinct accepted MIME types."""

    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"


@dataclass(frozen=True, slots=True)
class MediaMetadata:
    """Stable metadata captured when Telegram first exposes a file."""

    mime_type: str
    file_size: int | None = None
    file_name: str | None = None
    width: int | None = None
    height: int | None = None
    duration_seconds: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "mime_type", normalize_mime_type(self.mime_type))
        if self.file_size is not None and self.file_size < 0:
            raise ValueError("file_size must not be negative")
        for field_name in ("width", "height", "duration_seconds"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} must not be negative")
        if self.file_name is not None and not self.file_name.strip():
            raise ValueError("file_name must not be blank")


@dataclass(frozen=True, slots=True)
class MediaReference:
    """Stable Telegram media identity without bytes or a signed file URL."""

    file_id: str
    file_unique_id: str
    metadata: MediaMetadata

    def __post_init__(self) -> None:
        if not self.file_id.strip():
            raise ValueError("file_id must not be empty")
        if not self.file_unique_id.strip():
            raise ValueError("file_unique_id must not be empty")


__all__ = [
    "MediaFamily",
    "MediaMetadata",
    "MediaReference",
    "normalize_mime_type",
]
