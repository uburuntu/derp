"""Async private-filesystem storage for short-lived generated artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import stat
import struct
import uuid
from pathlib import Path
from typing import Final, Protocol

from derp.artifacts.types import (
    ArtifactIntegrityError,
    ArtifactKey,
    ArtifactKind,
    ArtifactMetadata,
    ArtifactNotFoundError,
    ArtifactStoreError,
    ArtifactTooLargeError,
    InvalidArtifactKeyError,
    StoredArtifact,
)

_MAGIC: Final = b"DERP-ARTIFACT\x00\x01"
_HEADER_LENGTH: Final = struct.Struct(">I")
_MAX_HEADER_BYTES: Final = 4096
_FILE_SUFFIX: Final = ".artifact"
DEFAULT_MAX_ITEM_BYTES: Final = 50 * 1024 * 1024


class ArtifactStore(Protocol):
    """Minimal async boundary consumed by delivery orchestration."""

    async def put(
        self,
        *,
        kind: ArtifactKind,
        mime_type: str,
        data: bytes,
    ) -> ArtifactMetadata:
        """Persist one bounded artifact."""
        ...

    async def read(self, key: ArtifactKey | str) -> StoredArtifact:
        """Read and verify one artifact."""
        ...

    async def delete(self, key: ArtifactKey | str) -> bool:
        """Idempotently remove one artifact."""
        ...


class FilesystemArtifactStore:
    """Store opaque artifacts atomically beneath one private local root."""

    def __init__(
        self,
        root: str | Path,
        *,
        max_item_bytes: int = DEFAULT_MAX_ITEM_BYTES,
    ) -> None:
        if max_item_bytes <= 0:
            raise ValueError("max_item_bytes must be positive")
        root_path = Path(root).expanduser()
        if not root_path.name:
            raise ValueError("artifact root must identify a directory")
        self._root = root_path.absolute()
        self._max_item_bytes = max_item_bytes

    async def put(
        self,
        *,
        kind: ArtifactKind,
        mime_type: str,
        data: bytes,
    ) -> ArtifactMetadata:
        """Atomically persist one bounded artifact and return opaque metadata."""
        if not isinstance(data, bytes):
            raise TypeError("artifact data must be immutable bytes")
        if not data:
            raise ValueError("artifact data must not be empty")
        if len(data) > self._max_item_bytes:
            raise ArtifactTooLargeError(
                size_bytes=len(data),
                limit_bytes=self._max_item_bytes,
            )
        key = ArtifactKey.new()
        metadata = ArtifactMetadata(
            key=key,
            kind=kind,
            mime_type=mime_type,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )
        try:
            await asyncio.to_thread(self._put_sync, metadata, data)
        except OSError as exc:
            raise ArtifactStoreError("Artifact write failed") from exc
        return metadata

    async def read(self, key: ArtifactKey | str) -> StoredArtifact:
        """Read one artifact only after metadata, size, and digest verification."""
        normalized = self._coerce_key(key)
        return await asyncio.to_thread(self._read_sync, normalized)

    async def delete(self, key: ArtifactKey | str) -> bool:
        """Idempotently remove an artifact, returning whether it existed."""
        normalized = self._coerce_key(key)
        try:
            return await asyncio.to_thread(self._delete_sync, normalized)
        except OSError as exc:
            raise ArtifactStoreError("Artifact deletion failed") from exc

    def _put_sync(self, metadata: ArtifactMetadata, data: bytes) -> None:
        self._ensure_private_root()
        target = self._path_for(metadata.key)
        temporary = self._root / f".{uuid.uuid4().hex}.tmp"
        header = self._encode_header(metadata)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as artifact_file:
                artifact_file.write(_MAGIC)
                artifact_file.write(_HEADER_LENGTH.pack(len(header)))
                artifact_file.write(header)
                artifact_file.write(data)
                artifact_file.flush()
                os.fsync(artifact_file.fileno())
            os.replace(temporary, target)
            os.chmod(target, 0o600, follow_symlinks=False)
            self._sync_directory()
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _read_sync(self, key: ArtifactKey) -> StoredArtifact:
        self._reject_symlink_root()
        path = self._path_for(key)
        if path.is_symlink():
            raise ArtifactIntegrityError("Artifact path is not a regular file")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError("Artifact does not exist") from exc
        except OSError as exc:
            raise ArtifactIntegrityError("Artifact path is not a regular file") from exc

        try:
            with os.fdopen(descriptor, "rb") as artifact_file:
                file_stat = os.fstat(artifact_file.fileno())
                if not stat.S_ISREG(file_stat.st_mode):
                    raise ArtifactIntegrityError("Artifact path is not a regular file")
                if stat.S_IMODE(file_stat.st_mode) != 0o600:
                    raise ArtifactIntegrityError(
                        "Artifact file permissions are invalid"
                    )
                if artifact_file.read(len(_MAGIC)) != _MAGIC:
                    raise ArtifactIntegrityError("Artifact file header is invalid")
                length_bytes = artifact_file.read(_HEADER_LENGTH.size)
                if len(length_bytes) != _HEADER_LENGTH.size:
                    raise ArtifactIntegrityError("Artifact metadata is truncated")
                header_size = _HEADER_LENGTH.unpack(length_bytes)[0]
                if not 0 < header_size <= _MAX_HEADER_BYTES:
                    raise ArtifactIntegrityError("Artifact metadata size is invalid")
                header = artifact_file.read(header_size)
                if len(header) != header_size:
                    raise ArtifactIntegrityError("Artifact metadata is truncated")
                metadata = self._decode_header(key, header)
                if metadata.size_bytes > self._max_item_bytes:
                    raise ArtifactIntegrityError(
                        "Artifact exceeds the configured limit"
                    )
                data = artifact_file.read(metadata.size_bytes)
                if len(data) != metadata.size_bytes or artifact_file.read(1):
                    raise ArtifactIntegrityError(
                        "Artifact byte size does not match metadata"
                    )
        except ArtifactIntegrityError:
            raise
        except (OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
            raise ArtifactIntegrityError("Artifact metadata is invalid") from exc

        digest = hashlib.sha256(data).hexdigest()
        if not hmac.compare_digest(digest, metadata.sha256):
            raise ArtifactIntegrityError("Artifact SHA-256 verification failed")
        return StoredArtifact(metadata=metadata, data=data)

    def _delete_sync(self, key: ArtifactKey) -> bool:
        self._reject_symlink_root()
        path = self._path_for(key)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        self._sync_directory()
        return True

    def _ensure_private_root(self) -> None:
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._root.is_symlink() or not self._root.is_dir():
            raise ArtifactIntegrityError("Artifact root must be a real directory")
        os.chmod(self._root, 0o700, follow_symlinks=False)

    def _reject_symlink_root(self) -> None:
        if self._root.is_symlink():
            raise ArtifactIntegrityError("Artifact root must be a real directory")

    def _path_for(self, key: ArtifactKey) -> Path:
        filename = f"{key.value.hex}{_FILE_SUFFIX}"
        path = self._root / filename
        if path.parent != self._root or path.name != filename:
            raise InvalidArtifactKeyError("Artifact key cannot address a path")
        return path

    @staticmethod
    def _coerce_key(key: ArtifactKey | str) -> ArtifactKey:
        if isinstance(key, ArtifactKey):
            return key
        if isinstance(key, str):
            return ArtifactKey.parse(key)
        raise InvalidArtifactKeyError("Artifact key must be an ArtifactKey or string")

    @staticmethod
    def _encode_header(metadata: ArtifactMetadata) -> bytes:
        encoded = json.dumps(
            {
                "version": 1,
                "key": str(metadata.key),
                "kind": metadata.kind.value,
                "mime_type": metadata.mime_type,
                "size_bytes": metadata.size_bytes,
                "sha256": metadata.sha256,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        if len(encoded) > _MAX_HEADER_BYTES:  # pragma: no cover - fixed schema bound
            raise ValueError("Artifact metadata exceeds its format limit")
        return encoded

    @staticmethod
    def _decode_header(key: ArtifactKey, header: bytes) -> ArtifactMetadata:
        raw = json.loads(header.decode("ascii"))
        expected_fields = {
            "version",
            "key",
            "kind",
            "mime_type",
            "size_bytes",
            "sha256",
        }
        if not isinstance(raw, dict) or set(raw) != expected_fields:
            raise ArtifactIntegrityError("Artifact metadata fields are invalid")
        if type(raw["version"]) is not int or raw["version"] != 1:
            raise ArtifactIntegrityError("Artifact metadata version is invalid")
        if raw["key"] != str(key):
            raise ArtifactIntegrityError("Artifact metadata identity is invalid")
        return ArtifactMetadata(
            key=key,
            kind=ArtifactKind(raw["kind"]),
            mime_type=raw["mime_type"],
            size_bytes=raw["size_bytes"],
            sha256=raw["sha256"],
        )

    def _sync_directory(self) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        descriptor = os.open(self._root, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = [
    "DEFAULT_MAX_ITEM_BYTES",
    "ArtifactStore",
    "FilesystemArtifactStore",
]
