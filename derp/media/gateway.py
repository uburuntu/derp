"""Bounded, privacy-safe transport for Telegram media bytes."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Mapping, Set
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

import httpx
import logfire
from aiogram import Bot

from derp.media.types import (
    MediaFamily,
    MediaReference,
    normalize_mime_type,
)

DEFAULT_ALLOWED_MIME_TYPES: Mapping[MediaFamily, frozenset[str]] = MappingProxyType(
    {
        MediaFamily.IMAGE: frozenset(
            {
                "image/bmp",
                "image/gif",
                "image/jpeg",
                "image/png",
                "image/tiff",
                "image/webp",
            }
        ),
        MediaFamily.AUDIO: frozenset(
            {
                "audio/aac",
                "audio/flac",
                "audio/mp4",
                "audio/mpeg",
                "audio/ogg",
                "audio/opus",
                "audio/wav",
                "audio/webm",
                "audio/x-m4a",
                "audio/x-wav",
            }
        ),
        MediaFamily.VIDEO: frozenset(
            {
                "video/mp4",
                "video/mpeg",
                "video/quicktime",
                "video/webm",
                "video/x-matroska",
            }
        ),
        MediaFamily.DOCUMENT: frozenset({"application/pdf"}),
    }
)


@dataclass(frozen=True, slots=True)
class MediaTimeoutPolicy:
    """Network phase limits plus an end-to-end operation deadline."""

    connect_seconds: float = 5.0
    read_seconds: float = 15.0
    total_seconds: float = 30.0

    def __post_init__(self) -> None:
        for field_name in ("connect_seconds", "read_seconds", "total_seconds"):
            value = getattr(self, field_name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{field_name} must be finite and positive")

    def as_httpx(self) -> httpx.Timeout:
        """Build strict HTTPX connect, read, write, and pool limits."""
        return httpx.Timeout(
            connect=self.connect_seconds,
            read=self.read_seconds,
            write=self.connect_seconds,
            pool=self.connect_seconds,
        )


@dataclass(frozen=True, slots=True)
class MediaDownloadPolicy:
    """Resource limits applied to every media hydration."""

    max_bytes: int = 20_000_000
    chunk_size: int = 64 * 1024
    timeouts: MediaTimeoutPolicy = MediaTimeoutPolicy()

    def __post_init__(self) -> None:
        if self.max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if self.chunk_size <= 0:
            raise ValueError("chunk_size must be positive")


class MediaGatewayError(RuntimeError):
    """Base class for safe errors that never contain a Telegram file URL."""


class UnsupportedMediaTypeError(MediaGatewayError):
    """The declared MIME type is outside the expected family's allowlist."""

    def __init__(self, family: MediaFamily, mime_type: str) -> None:
        self.family = family
        self.mime_type = mime_type
        super().__init__("Media type is not allowed for the expected family")


class MediaTooLargeError(MediaGatewayError):
    """Declared or streamed bytes exceed the configured transport cap."""

    def __init__(self, limit_bytes: int, declared_bytes: int | None = None) -> None:
        self.limit_bytes = limit_bytes
        self.declared_bytes = declared_bytes
        super().__init__("Media exceeds the download size limit")


class MediaTimeoutError(MediaGatewayError):
    """The end-to-end or HTTP phase timeout expired."""

    def __init__(self) -> None:
        super().__init__("Media download timed out")


class MediaUnavailableError(MediaGatewayError):
    """Telegram did not provide a usable file location."""

    def __init__(self) -> None:
        super().__init__("Media is unavailable")


class MediaReferenceMismatchError(MediaGatewayError):
    """Telegram resolved a different stable file identity."""

    def __init__(self) -> None:
        super().__init__("Resolved media does not match its reference")


class InvalidMediaResponseError(MediaGatewayError):
    """The file response contains malformed transport metadata."""

    def __init__(self) -> None:
        super().__init__("Media server returned an invalid response")


class MediaTransportError(MediaGatewayError):
    """Telegram resolution or HTTP transfer failed."""

    def __init__(self, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__("Media download failed")


class _FailureReason(StrEnum):
    UNSUPPORTED_TYPE = "unsupported_type"
    TOO_LARGE = "too_large"
    UNAVAILABLE = "unavailable"
    REFERENCE_MISMATCH = "reference_mismatch"
    INVALID_RESPONSE = "invalid_response"
    TRANSPORT = "transport"
    TIMEOUT = "timeout"


@dataclass(frozen=True, slots=True)
class _Failure:
    reason: _FailureReason
    mime_type: str | None = None
    declared_bytes: int | None = None
    status_code: int | None = None
    source_error_type: str | None = None


type _Attempt = bytes | _Failure


class MediaGateway:
    """Hydrate Telegram references through a caller-owned shared HTTP client."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        policy: MediaDownloadPolicy | None = None,
        allowed_mime_types: Mapping[MediaFamily, Set[str]] = (
            DEFAULT_ALLOWED_MIME_TYPES
        ),
    ) -> None:
        self._client = client
        self._policy = policy or MediaDownloadPolicy()
        self._allowed_mime_types = self._validate_allowlist(allowed_mime_types)

    async def download(
        self,
        *,
        bot: Bot,
        reference: MediaReference,
        expected_family: MediaFamily,
    ) -> bytes:
        """Resolve and download one reference without retaining its bytes."""
        if not isinstance(expected_family, MediaFamily):
            raise TypeError("expected_family must be a MediaFamily")

        attributes: dict[str, str | int] = {
            "media.family": expected_family.value,
            "media.mime_type": reference.metadata.mime_type,
            "media.max_bytes": self._policy.max_bytes,
        }
        if reference.metadata.file_size is not None:
            attributes["media.file_size"] = reference.metadata.file_size

        with logfire.span("media.telegram_download", **attributes) as span:
            attempt = self._validate_reference(reference, expected_family)
            if attempt is None:
                try:
                    async with asyncio.timeout(self._policy.timeouts.total_seconds):
                        attempt = await self._resolve_and_fetch(
                            bot, reference, expected_family
                        )
                except (TimeoutError, httpx.TimeoutException) as exc:
                    attempt = _Failure(
                        _FailureReason.TIMEOUT,
                        source_error_type=type(exc).__name__,
                    )
                except Exception as exc:
                    attempt = _Failure(
                        _FailureReason.TRANSPORT,
                        source_error_type=type(exc).__name__,
                    )

            if isinstance(attempt, bytes):
                span.set_attribute("media.downloaded_bytes", len(attempt))
                span.set_attribute("media.outcome", "success")
                return attempt

            error = self._to_error(attempt, expected_family)
            span.set_attribute("media.outcome", attempt.reason.value)
            span.set_attribute(
                "error.type", attempt.source_error_type or type(error).__name__
            )
            if attempt.status_code is not None:
                span.set_attribute("http.response.status_code", attempt.status_code)
            raise error

    def _validate_reference(
        self,
        reference: MediaReference,
        expected_family: MediaFamily,
    ) -> _Failure | None:
        if (
            reference.metadata.mime_type
            not in self._allowed_mime_types[expected_family]
        ):
            return _Failure(
                _FailureReason.UNSUPPORTED_TYPE,
                mime_type=reference.metadata.mime_type,
            )
        if (
            reference.metadata.file_size is not None
            and reference.metadata.file_size > self._policy.max_bytes
        ):
            return _Failure(
                _FailureReason.TOO_LARGE,
                declared_bytes=reference.metadata.file_size,
            )
        return None

    async def _resolve_and_fetch(
        self,
        bot: Bot,
        reference: MediaReference,
        expected_family: MediaFamily,
    ) -> _Attempt:
        telegram_file = await bot.get_file(
            reference.file_id,
            request_timeout=max(1, math.ceil(self._policy.timeouts.total_seconds)),
        )
        if telegram_file.file_unique_id != reference.file_unique_id:
            return _Failure(_FailureReason.REFERENCE_MISMATCH)
        if not telegram_file.file_path:
            return _Failure(_FailureReason.UNAVAILABLE)
        if (
            telegram_file.file_size is not None
            and telegram_file.file_size > self._policy.max_bytes
        ):
            return _Failure(
                _FailureReason.TOO_LARGE,
                declared_bytes=telegram_file.file_size,
            )

        signed_url = bot.session.api.file_url(bot.token, telegram_file.file_path)
        return await self._fetch(signed_url, expected_family)

    async def _fetch(self, signed_url: str, expected_family: MediaFamily) -> _Attempt:
        async with self._client.stream(
            "GET",
            signed_url,
            timeout=self._policy.timeouts.as_httpx(),
        ) as response:
            if not response.is_success:
                return _Failure(
                    _FailureReason.TRANSPORT,
                    status_code=response.status_code,
                )

            if isinstance(
                failure := self._validate_headers(response, expected_family), _Failure
            ):
                return failure

            content = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=self._policy.chunk_size):
                if len(content) + len(chunk) > self._policy.max_bytes:
                    return _Failure(_FailureReason.TOO_LARGE)
                content.extend(chunk)
            return bytes(content)

    def _validate_headers(
        self,
        response: httpx.Response,
        expected_family: MediaFamily,
    ) -> _Failure | None:
        content_length = self._content_length(response.headers)
        if content_length is False:
            return _Failure(_FailureReason.INVALID_RESPONSE)
        if isinstance(content_length, int) and content_length > self._policy.max_bytes:
            return _Failure(
                _FailureReason.TOO_LARGE,
                declared_bytes=content_length,
            )

        if declared_mime := response.headers.get("content-type"):
            try:
                response_mime = normalize_mime_type(declared_mime)
            except ValueError:
                return _Failure(_FailureReason.INVALID_RESPONSE)
            if response_mime not in self._allowed_mime_types[expected_family]:
                return _Failure(
                    _FailureReason.UNSUPPORTED_TYPE,
                    mime_type=response_mime,
                )
        return None

    @staticmethod
    def _content_length(headers: httpx.Headers) -> int | None | bool:
        values = [
            part.strip()
            for value in headers.get_list("content-length")
            for part in value.split(",")
        ]
        if not values:
            return None
        if len(set(values)) != 1 or not values[0].isdecimal():
            return False
        return int(values[0])

    @staticmethod
    def _validate_allowlist(
        allowed_mime_types: Mapping[MediaFamily, Set[str]],
    ) -> Mapping[MediaFamily, frozenset[str]]:
        if set(allowed_mime_types) != set(MediaFamily):
            raise ValueError("allowed_mime_types must define every MediaFamily")
        normalized = {
            family: frozenset(normalize_mime_type(value) for value in values)
            for family, values in allowed_mime_types.items()
        }
        if any(not values for values in normalized.values()):
            raise ValueError("allowed_mime_types entries must not be empty")
        return MappingProxyType(normalized)

    def _to_error(
        self,
        failure: _Failure,
        expected_family: MediaFamily,
    ) -> MediaGatewayError:
        match failure.reason:
            case _FailureReason.UNSUPPORTED_TYPE:
                return UnsupportedMediaTypeError(
                    expected_family,
                    failure.mime_type or "application/octet-stream",
                )
            case _FailureReason.TOO_LARGE:
                return MediaTooLargeError(
                    self._policy.max_bytes,
                    failure.declared_bytes,
                )
            case _FailureReason.TIMEOUT:
                return MediaTimeoutError()
            case _FailureReason.UNAVAILABLE:
                return MediaUnavailableError()
            case _FailureReason.REFERENCE_MISMATCH:
                return MediaReferenceMismatchError()
            case _FailureReason.INVALID_RESPONSE:
                return InvalidMediaResponseError()
            case _FailureReason.TRANSPORT:
                return MediaTransportError(failure.status_code)


__all__ = [
    "DEFAULT_ALLOWED_MIME_TYPES",
    "InvalidMediaResponseError",
    "MediaDownloadPolicy",
    "MediaGateway",
    "MediaGatewayError",
    "MediaReferenceMismatchError",
    "MediaTimeoutError",
    "MediaTimeoutPolicy",
    "MediaTooLargeError",
    "MediaTransportError",
    "MediaUnavailableError",
    "UnsupportedMediaTypeError",
]
