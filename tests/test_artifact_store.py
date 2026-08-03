"""Contracts for private atomic filesystem artifact storage."""

from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from derp.artifacts import (
    ArtifactIntegrityError,
    ArtifactKey,
    ArtifactKind,
    ArtifactNotFoundError,
    ArtifactStoreError,
    ArtifactTooLargeError,
    FilesystemArtifactStore,
    InvalidArtifactKeyError,
)


def _artifact_path(root: Path) -> Path:
    files = list(root.iterdir())
    assert len(files) == 1
    return files[0]


@pytest.mark.asyncio
async def test_private_atomic_round_trip_uses_opaque_metadata(tmp_path: Path) -> None:
    root = tmp_path / "private-artifacts"
    store = FilesystemArtifactStore(root, max_item_bytes=1024)
    data = b"generated-image"

    metadata = await store.put(
        kind=ArtifactKind.IMAGE,
        mime_type="IMAGE/PNG; charset=binary",
        data=data,
    )

    assert isinstance(metadata.key.value, uuid.UUID)
    assert metadata.kind is ArtifactKind.IMAGE
    assert metadata.mime_type == "image/png"
    assert metadata.size_bytes == len(data)
    assert len(metadata.sha256) == 64
    assert not hasattr(metadata, "url")
    assert not hasattr(metadata, "path")

    artifact_path = _artifact_path(root)
    assert artifact_path.name == f"{metadata.key.value.hex}.artifact"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o600
    assert not any(path.suffix == ".tmp" for path in root.iterdir())

    stored = await store.read(str(metadata.key))

    assert stored.metadata == metadata
    assert stored.data == data


@pytest.mark.asyncio
async def test_put_rejects_oversized_or_mismatched_media_before_writing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    store = FilesystemArtifactStore(root, max_item_bytes=3)

    with pytest.raises(ArtifactTooLargeError) as error:
        await store.put(
            kind=ArtifactKind.IMAGE,
            mime_type="image/png",
            data=b"1234",
        )
    assert error.value.size_bytes == 4
    assert error.value.limit_bytes == 3

    with pytest.raises(ValueError, match="match its kind"):
        await store.put(
            kind=ArtifactKind.IMAGE,
            mime_type="audio/ogg",
            data=b"123",
        )
    assert not root.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malicious_key",
    [
        "../secret",
        "../../etc/passwd",
        "/absolute/path",
        "00000000000000000000000000000000",
        "{00000000-0000-0000-0000-000000000000}",
    ],
)
async def test_external_keys_reject_traversal_and_noncanonical_forms(
    tmp_path: Path,
    malicious_key: str,
) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")

    with pytest.raises(InvalidArtifactKeyError):
        await store.read(malicious_key)
    with pytest.raises(InvalidArtifactKeyError):
        await store.delete(malicious_key)


@pytest.mark.asyncio
async def test_delete_is_idempotent_and_read_reports_missing(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    metadata = await store.put(
        kind=ArtifactKind.VOICE,
        mime_type="audio/ogg",
        data=b"voice",
    )

    assert await store.delete(metadata.key)
    assert not await store.delete(metadata.key)
    with pytest.raises(ArtifactNotFoundError):
        await store.read(metadata.key)


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["digest", "size"])
async def test_read_rejects_tampered_bytes(tmp_path: Path, tamper: str) -> None:
    root = tmp_path / "artifacts"
    store = FilesystemArtifactStore(root)
    metadata = await store.put(
        kind=ArtifactKind.VIDEO,
        mime_type="video/mp4",
        data=b"video-bytes",
    )
    path = _artifact_path(root)
    raw = bytearray(path.read_bytes())
    if tamper == "digest":
        raw[-1] ^= 1
    else:
        raw.pop()
    path.write_bytes(raw)
    path.chmod(0o600)

    with pytest.raises(ArtifactIntegrityError):
        await store.read(metadata.key)


@pytest.mark.asyncio
async def test_read_rejects_public_permissions(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = FilesystemArtifactStore(root)
    metadata = await store.put(
        kind=ArtifactKind.AUDIO,
        mime_type="audio/mpeg",
        data=b"audio",
    )
    _artifact_path(root).chmod(0o644)

    with pytest.raises(ArtifactIntegrityError, match="permissions"):
        await store.read(metadata.key)


@pytest.mark.asyncio
async def test_read_rejects_symlink_substitution(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = FilesystemArtifactStore(root)
    metadata = await store.put(
        kind=ArtifactKind.DOCUMENT,
        mime_type="application/pdf",
        data=b"document",
    )
    artifact_path = _artifact_path(root)
    artifact_path.unlink()
    outside = tmp_path / "outside"
    outside.write_bytes(b"private")
    artifact_path.symlink_to(outside)

    with pytest.raises(ArtifactIntegrityError, match="regular file"):
        await store.read(metadata.key)


@pytest.mark.asyncio
async def test_access_rejects_root_swapped_for_symlink(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = FilesystemArtifactStore(root)
    metadata = await store.put(
        kind=ArtifactKind.IMAGE,
        mime_type="image/png",
        data=b"image",
    )
    moved_root = tmp_path / "moved-artifacts"
    root.rename(moved_root)
    root.symlink_to(moved_root, target_is_directory=True)

    with pytest.raises(ArtifactIntegrityError, match="real directory"):
        await store.read(metadata.key)
    with pytest.raises(ArtifactIntegrityError, match="real directory"):
        await store.delete(metadata.key)


@pytest.mark.asyncio
async def test_failed_atomic_publish_removes_temporary_file(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = FilesystemArtifactStore(root)

    with (
        patch("derp.artifacts.store.os.replace", side_effect=OSError("disk error")),
        pytest.raises(ArtifactStoreError, match="write failed"),
    ):
        await store.put(
            kind=ArtifactKind.IMAGE,
            mime_type="image/png",
            data=b"image",
        )

    assert root.exists()
    assert list(root.iterdir()) == []


def test_artifact_key_parser_accepts_only_canonical_uuid() -> None:
    key = ArtifactKey.new()

    assert ArtifactKey.parse(str(key)) == key
    with pytest.raises(InvalidArtifactKeyError):
        ArtifactKey.parse(str(key).upper())


def test_store_configuration_requires_real_bounded_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        FilesystemArtifactStore(tmp_path / "artifacts", max_item_bytes=0)
    with pytest.raises(ValueError, match="identify a directory"):
        FilesystemArtifactStore(os.sep)
