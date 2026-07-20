"""Typed values for private generated-artifact storage."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from enum import StrEnum

from derp.media.types import normalize_mime_type


class ArtifactKind(StrEnum):
    """Delivery-relevant generated media categories."""

    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    DOCUMENT = "document"

    @property
    def mime_prefix(self) -> str:
        """Return the MIME top-level type allowed for this artifact kind."""
        if self in {ArtifactKind.AUDIO, ArtifactKind.VOICE}:
            return "audio/"
        if self is ArtifactKind.DOCUMENT:
            return "application/"
        return f"{self.value}/"


class ArtifactStoreError(RuntimeError):
    """Base class for safe local artifact failures."""


class InvalidArtifactKeyError(ArtifactStoreError, ValueError):
    """An external key is not one canonical opaque UUID."""


class ArtifactNotFoundError(ArtifactStoreError, FileNotFoundError):
    """No artifact exists for the requested opaque key."""


class ArtifactIntegrityError(ArtifactStoreError):
    """Stored bytes or metadata fail integrity validation."""


class ArtifactTooLargeError(ArtifactStoreError, ValueError):
    """Artifact bytes exceed the configured per-item limit."""

    def __init__(self, *, size_bytes: int, limit_bytes: int) -> None:
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes
        super().__init__("Artifact exceeds the per-item byte limit")


@dataclass(frozen=True, slots=True)
class ArtifactKey:
    """Opaque UUID identity with no path or URL semantics."""

    value: uuid.UUID

    def __post_init__(self) -> None:
        if not isinstance(self.value, uuid.UUID):
            raise TypeError("artifact key value must be a UUID")

    @classmethod
    def new(cls) -> ArtifactKey:
        """Generate an unpredictable storage identity."""
        return cls(uuid.uuid4())

    @classmethod
    def parse(cls, value: str) -> ArtifactKey:
        """Parse only the canonical UUID representation used by callbacks."""
        if not isinstance(value, str):
            raise InvalidArtifactKeyError("Artifact key must be a string")
        try:
            parsed = uuid.UUID(value)
        except (ValueError, AttributeError) as exc:
            raise InvalidArtifactKeyError(
                "Artifact key must be a canonical UUID"
            ) from exc
        if value != str(parsed):
            raise InvalidArtifactKeyError("Artifact key must be a canonical UUID")
        return cls(parsed)

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    """Durable, content-verifiable metadata without a filesystem location."""

    key: ArtifactKey
    kind: ArtifactKind
    mime_type: str
    size_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, ArtifactKey):
            raise TypeError("artifact metadata key must be an ArtifactKey")
        if not isinstance(self.kind, ArtifactKind):
            raise TypeError("artifact metadata kind must be an ArtifactKind")
        mime_type = normalize_mime_type(self.mime_type)
        if not mime_type.startswith(self.kind.mime_prefix):
            raise ValueError("artifact MIME type must match its kind")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int):
            raise TypeError("artifact size must be an integer")
        if self.size_bytes <= 0:
            raise ValueError("artifact size must be positive")
        if not isinstance(self.sha256, str):
            raise TypeError("artifact SHA-256 must be a string")
        if len(self.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.sha256
        ):
            raise ValueError("artifact SHA-256 must be lowercase hexadecimal")
        object.__setattr__(self, "mime_type", mime_type)


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    """Verified metadata and bytes returned from private storage."""

    metadata: ArtifactMetadata
    data: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, ArtifactMetadata):
            raise TypeError("stored artifact metadata must be ArtifactMetadata")
        if not isinstance(self.data, bytes):
            raise TypeError("stored artifact data must be immutable bytes")
        if len(self.data) != self.metadata.size_bytes:
            raise ArtifactIntegrityError("stored artifact size does not match metadata")
        if not hmac.compare_digest(
            hashlib.sha256(self.data).hexdigest(),
            self.metadata.sha256,
        ):
            raise ArtifactIntegrityError(
                "stored artifact digest does not match metadata"
            )


__all__ = [
    "ArtifactIntegrityError",
    "ArtifactKey",
    "ArtifactKind",
    "ArtifactMetadata",
    "ArtifactNotFoundError",
    "ArtifactStoreError",
    "ArtifactTooLargeError",
    "InvalidArtifactKeyError",
    "StoredArtifact",
]
