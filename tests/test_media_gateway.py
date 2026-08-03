"""Tests for bounded Telegram media hydration."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram import Bot
from aiogram.types import File
from logfire.testing import CaptureLogfire

from derp.media import (
    InvalidMediaResponseError,
    MediaDownloadPolicy,
    MediaFamily,
    MediaGateway,
    MediaMetadata,
    MediaReference,
    MediaReferenceMismatchError,
    MediaTimeoutError,
    MediaTimeoutPolicy,
    MediaTooLargeError,
    MediaTransportError,
    UnsupportedMediaTypeError,
)

TOKEN_SENTINEL = "123456789:private-token-value-sentinel"
SIGNED_URL = f"https://api.telegram.org/file/bot{TOKEN_SENTINEL}/photos/file.jpg"


class TrackingStream(httpx.AsyncByteStream):
    """Expose whether a response body was consumed."""

    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks
        self.started = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started = True
        for chunk in self.chunks:
            yield chunk


def make_reference(
    *,
    mime_type: str = "image/jpeg",
    file_size: int | None = None,
) -> MediaReference:
    return MediaReference(
        file_id="telegram-file-id",
        file_unique_id="stable-file-id",
        metadata=MediaMetadata(
            mime_type=mime_type,
            file_size=file_size,
            file_name="photo.jpg",
            width=640,
            height=480,
        ),
    )


def make_bot(
    *,
    file_size: int | None = None,
    file_path: str | None = "photos/file.jpg",
    file_unique_id: str = "stable-file-id",
) -> tuple[Bot, AsyncMock, MagicMock]:
    bot = MagicMock(spec=Bot)
    get_file = AsyncMock(
        return_value=File(
            file_id="telegram-file-id",
            file_unique_id=file_unique_id,
            file_size=file_size,
            file_path=file_path,
        )
    )
    file_url = MagicMock(return_value=SIGNED_URL)
    bot.get_file = get_file
    bot.token = TOKEN_SENTINEL
    bot.session = MagicMock()
    bot.session.api.file_url = file_url
    return cast(Bot, bot), get_file, file_url


async def call_gateway(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    reference: MediaReference | None = None,
    bot: Bot | None = None,
    expected_family: MediaFamily = MediaFamily.IMAGE,
    policy: MediaDownloadPolicy | None = None,
) -> bytes:
    resolved_bot = bot or make_bot()[0]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await MediaGateway(client, policy=policy).download(
            bot=resolved_bot,
            reference=reference or make_reference(),
            expected_family=expected_family,
        )


@pytest.mark.asyncio
async def test_download_resolves_then_streams_with_shared_client() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-length": "6", "content-type": "image/jpeg"},
            content=b"abcdef",
        )

    bot, get_file, file_url = make_bot(file_size=6)
    timeout_policy = MediaTimeoutPolicy(
        connect_seconds=1.5,
        read_seconds=2.5,
        total_seconds=8.0,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = MediaGateway(
            client,
            policy=MediaDownloadPolicy(max_bytes=10, timeouts=timeout_policy),
        )

        first = await gateway.download(
            bot=bot,
            reference=make_reference(file_size=6),
            expected_family=MediaFamily.IMAGE,
        )
        second = await gateway.download(
            bot=bot,
            reference=make_reference(file_size=6),
            expected_family=MediaFamily.IMAGE,
        )

        assert first == second == b"abcdef"
        assert not client.is_closed

    assert get_file.await_count == 2
    get_file.assert_awaited_with("telegram-file-id", request_timeout=8)
    file_url.assert_called_with(TOKEN_SENTINEL, "photos/file.jpg")
    assert [str(request.url) for request in requests] == [SIGNED_URL, SIGNED_URL]
    assert requests[0].extensions["timeout"] == {
        "connect": 1.5,
        "read": 2.5,
        "write": 1.5,
        "pool": 1.5,
    }


@pytest.mark.asyncio
async def test_declared_mime_is_rejected_before_telegram_io() -> None:
    bot, get_file, _ = make_bot()

    with pytest.raises(UnsupportedMediaTypeError) as exc_info:
        await call_gateway(
            lambda request: httpx.Response(200, content=b"unused"),
            bot=bot,
            reference=make_reference(mime_type="text/plain"),
        )

    assert exc_info.value.family is MediaFamily.IMAGE
    assert exc_info.value.mime_type == "text/plain"
    get_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_metadata_size_is_rejected_before_telegram_io() -> None:
    bot, get_file, _ = make_bot()

    with pytest.raises(MediaTooLargeError) as exc_info:
        await call_gateway(
            lambda request: httpx.Response(200, content=b"unused"),
            bot=bot,
            reference=make_reference(file_size=11),
            policy=MediaDownloadPolicy(max_bytes=10),
        )

    assert exc_info.value.declared_bytes == 11
    get_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_content_length_is_checked_before_body_iteration() -> None:
    stream = TrackingStream(b"body was not read")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": "11", "content-type": "image/jpeg"},
            stream=stream,
        )

    with pytest.raises(MediaTooLargeError) as exc_info:
        await call_gateway(
            handler,
            policy=MediaDownloadPolicy(max_bytes=10),
        )

    assert exc_info.value.declared_bytes == 11
    assert not stream.started


@pytest.mark.asyncio
async def test_streaming_cap_applies_without_content_length() -> None:
    stream = TrackingStream(b"1234", b"5678", b"9")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg"},
            stream=stream,
        )

    with pytest.raises(MediaTooLargeError) as exc_info:
        await call_gateway(
            handler,
            policy=MediaDownloadPolicy(max_bytes=8, chunk_size=4),
        )

    assert exc_info.value.declared_bytes is None
    assert stream.started


@pytest.mark.asyncio
async def test_response_mime_must_match_expected_family() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"not an image",
        )

    with pytest.raises(UnsupportedMediaTypeError) as exc_info:
        await call_gateway(handler)

    assert exc_info.value.mime_type == "text/html"


@pytest.mark.asyncio
async def test_malformed_content_length_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-length": "unknown", "content-type": "image/jpeg"},
            content=b"abc",
        )

    with pytest.raises(InvalidMediaResponseError):
        await call_gateway(handler)


@pytest.mark.asyncio
async def test_stable_identity_must_match_get_file_result() -> None:
    bot, _, file_url = make_bot(file_unique_id="different-stable-id")

    with pytest.raises(MediaReferenceMismatchError):
        await call_gateway(
            lambda request: httpx.Response(200, content=b"unused"),
            bot=bot,
        )

    file_url.assert_not_called()


@pytest.mark.asyncio
async def test_end_to_end_deadline_covers_get_file() -> None:
    bot, get_file, _ = make_bot()

    async def wait_forever(*args: object, **kwargs: object) -> File:
        await asyncio.sleep(60)
        raise AssertionError("unreachable")

    get_file.side_effect = wait_forever
    policy = MediaDownloadPolicy(
        timeouts=MediaTimeoutPolicy(
            connect_seconds=0.01,
            read_seconds=0.01,
            total_seconds=0.01,
        )
    )

    with pytest.raises(MediaTimeoutError):
        await call_gateway(
            lambda request: httpx.Response(200, content=b"unused"),
            bot=bot,
            policy=policy,
        )


@pytest.mark.asyncio
async def test_signed_url_is_absent_from_error_and_spans(
    capfire: CaptureLogfire,
) -> None:
    capfire.exporter.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"connection failed for {request.url}",
            request=request,
        )

    with pytest.raises(MediaTransportError) as exc_info:
        await call_gateway(handler)

    assert TOKEN_SENTINEL not in str(exc_info.value)
    assert TOKEN_SENTINEL not in repr(exc_info.value)
    assert exc_info.value.__context__ is None
    spans = capfire.exporter.exported_spans_as_dict()
    assert TOKEN_SENTINEL not in json.dumps(spans)
    span = next(span for span in spans if span["name"] == "media.telegram_download")
    assert span["attributes"]["media.outcome"] == "transport"
    assert span["attributes"]["error.type"] == "ConnectError"
    assert "url" not in span["attributes"]


def test_media_reference_is_frozen_and_normalizes_mime_metadata() -> None:
    reference = MediaReference(
        file_id="file-id",
        file_unique_id="unique-id",
        metadata=MediaMetadata(
            mime_type=" Image/JPEG; charset=binary ",
            file_size=3,
        ),
    )

    assert reference.metadata.mime_type == "image/jpeg"
    assert hash(reference)
    with pytest.raises(AttributeError):
        reference.file_id = "replacement"


@pytest.mark.parametrize(
    "timeouts",
    [
        MediaTimeoutPolicy(connect_seconds=1, read_seconds=1, total_seconds=1),
        MediaTimeoutPolicy(connect_seconds=0.5, read_seconds=2, total_seconds=3),
    ],
)
def test_timeout_policy_builds_all_httpx_phase_limits(
    timeouts: MediaTimeoutPolicy,
) -> None:
    timeout = timeouts.as_httpx()

    assert timeout.connect == timeouts.connect_seconds
    assert timeout.read == timeouts.read_seconds
    assert timeout.write == timeouts.connect_seconds
    assert timeout.pool == timeouts.connect_seconds
