"""Privacy-safe typed errors for direct OpenRouter HTTP calls."""

from __future__ import annotations

from enum import StrEnum


class OpenRouterError(RuntimeError):
    """Base error for direct OpenRouter transport failures."""


class TransportFailureKind(StrEnum):
    """Stable network failure categories without exception text."""

    TIMEOUT = "timeout"
    NETWORK = "network"


class ResponseFailureKind(StrEnum):
    """Stable response validation failures without response content."""

    EMPTY_BODY = "empty_body"
    INVALID_JSON = "invalid_json"
    INVALID_SCHEMA = "invalid_schema"
    INVALID_MEDIA = "invalid_media"
    MISSING_GENERATION_ID = "missing_generation_id"
    RESPONSE_TOO_LARGE = "response_too_large"
    UNEXPECTED_CONTENT_TYPE = "unexpected_content_type"


class OpenRouterTransportError(OpenRouterError):
    """OpenRouter could not be reached or timed out."""

    def __init__(
        self, *, kind: TransportFailureKind, method: str, endpoint: str
    ) -> None:
        self.kind = kind
        self.method = method
        self.endpoint = endpoint
        super().__init__(f"OpenRouter {method} {endpoint} failed: {kind.value}")


class OpenRouterHTTPError(OpenRouterError):
    """OpenRouter returned a non-success status without retaining its body."""

    def __init__(
        self,
        *,
        status_code: int,
        method: str,
        endpoint: str,
        error_code: int | str | None = None,
        retry_after: str | None = None,
        request_id: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.method = method
        self.endpoint = endpoint
        self.error_code = error_code
        self.retry_after = retry_after
        self.request_id = request_id
        super().__init__(f"OpenRouter {method} {endpoint} returned HTTP {status_code}")

    @property
    def retryable(self) -> bool:
        """Report retry eligibility without retrying automatically."""
        return self.status_code in {408, 409, 425, 429, 500, 502, 503, 504, 524, 529}


class OpenRouterResponseError(OpenRouterError):
    """A success response did not satisfy its documented wire contract."""

    def __init__(
        self,
        *,
        kind: ResponseFailureKind,
        endpoint: str,
    ) -> None:
        self.kind = kind
        self.endpoint = endpoint
        super().__init__(f"OpenRouter {endpoint} response failed: {kind.value}")


class OpenRouterJobError(OpenRouterError):
    """A video job reached a non-success terminal state."""

    def __init__(self, *, job_id: str, status: str) -> None:
        self.job_id = job_id
        self.status = status
        super().__init__(f"OpenRouter video job ended with status {status}")


class OpenRouterUsageError(OpenRouterError):
    """A response did not include usage required by its caller."""


class OpenRouterCostError(OpenRouterUsageError):
    """Usage did not include a finite, non-negative actual USD cost."""


__all__ = [
    "OpenRouterCostError",
    "OpenRouterError",
    "OpenRouterHTTPError",
    "OpenRouterJobError",
    "OpenRouterResponseError",
    "OpenRouterTransportError",
    "OpenRouterUsageError",
    "ResponseFailureKind",
    "TransportFailureKind",
]
