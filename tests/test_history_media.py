"""History media hydration shares bounded transport and degrades by item."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from derp.history.media import hydrate_media, hydration_candidate
from derp.history.snapshot import AttachmentMediaType, AttachmentSnapshot
from derp.media import MediaTooLargeError, MediaTransportError


def photo(file_id: str, *, size: int | None = 10) -> AttachmentSnapshot:
    return AttachmentSnapshot(
        media_type=AttachmentMediaType.PHOTO,
        file_id=file_id,
        file_unique_id=f"stable-{file_id}",
        size=size,
        width=100,
        height=80,
    )


def test_builds_transport_candidate_without_signed_url() -> None:
    candidate = hydration_candidate(photo("file"))

    assert candidate is not None
    assert candidate.media_reference.metadata.mime_type == "image/jpeg"
    assert candidate.history_reference.file_unique_id == "stable-file"
    assert "api.telegram" not in repr(candidate)


def test_rejects_non_pdf_document_from_model_hydration() -> None:
    attachment = AttachmentSnapshot(
        media_type=AttachmentMediaType.DOCUMENT,
        file_id="archive",
        file_unique_id="stable-archive",
        mime_type="application/zip",
    )

    assert hydration_candidate(attachment) is None


@pytest.mark.asyncio
async def test_hydrates_trimmed_items_and_keeps_partial_success() -> None:
    gateway = MagicMock()
    gateway.download = AsyncMock(
        side_effect=[b"second", MediaTransportError(status_code=503)]
    )
    candidates = [hydration_candidate(photo(str(index))) for index in range(3)]

    result = await hydrate_media(
        gateway=gateway,
        bot=MagicMock(),
        candidates=[item for item in candidates[-2:] if item is not None],
        max_items=2,
        concurrency=1,
    )

    assert gateway.download.await_count == 2
    assert len(result.content) == 1
    assert result.failures == 1
    reference, content = next(iter(result.content.items()))
    assert reference.file_id == "2"
    assert content.data == b"second"


@pytest.mark.asyncio
async def test_rejects_untrimmed_items_instead_of_silently_selecting_media() -> None:
    gateway = MagicMock()
    gateway.download = AsyncMock(return_value=b"123456")
    candidates = [hydration_candidate(photo(str(index), size=6)) for index in range(3)]

    with pytest.raises(ValueError, match="complete logical turn"):
        await hydrate_media(
            gateway=gateway,
            bot=MagicMock(),
            candidates=[item for item in candidates if item is not None],
            max_items=2,
            max_total_bytes=10,
            concurrency=2,
        )

    gateway.download.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_bytes_cannot_exceed_aggregate_budget() -> None:
    gateway = MagicMock()
    gateway.download = AsyncMock(return_value=b"123456")
    candidates = [hydration_candidate(photo(str(index), size=4)) for index in range(2)]

    result = await hydrate_media(
        gateway=gateway,
        bot=MagicMock(),
        candidates=[item for item in candidates if item is not None],
        max_items=2,
        max_total_bytes=8,
        concurrency=2,
    )

    assert gateway.download.await_count == 2
    assert [reference.file_id for reference in result.content] == ["1"]
    assert result.downloaded_bytes == 6
    assert sum(len(content.data) for content in result.content.values()) <= 8
    assert result.failures == 1


@pytest.mark.asyncio
async def test_oversize_download_retains_a_graceful_failure_marker() -> None:
    gateway = MagicMock()
    gateway.download = AsyncMock(side_effect=MediaTooLargeError(limit_bytes=5))
    candidate = hydration_candidate(photo("image", size=4))

    result = await hydrate_media(
        gateway=gateway,
        bot=MagicMock(),
        candidates=[candidate] if candidate is not None else [],
        max_items=1,
        max_total_bytes=8,
    )

    assert result.content == {}
    assert result.failures == 1
