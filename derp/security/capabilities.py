"""Restart-stable, domain-separated opaque capability tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from typing import Final

_ROOT_CONTEXT: Final = b"derp:capability:v1\x00"
_MINIMUM_SECRET_BYTES: Final = 32
_MAXIMUM_CONTEXT_BYTES: Final = 255


class CapabilityTokenCodec:
    """Derive one-way callback capabilities from durable object identities."""

    def __init__(self, secret: bytes, *, context: bytes) -> None:
        if not isinstance(secret, bytes):
            raise TypeError("capability secret must be bytes")
        if len(secret) < _MINIMUM_SECRET_BYTES:
            raise ValueError("capability secret must contain at least 32 bytes")
        if not isinstance(context, bytes):
            raise TypeError("capability context must be bytes")
        if not context:
            raise ValueError("capability context must not be empty")
        if len(context) > _MAXIMUM_CONTEXT_BYTES:
            raise ValueError("capability context is too long")
        self._secret = secret
        self._context = context

    def issue(self, subject_id: uuid.UUID) -> str:
        """Return a deterministic, URL-safe capability for one durable subject."""
        if not isinstance(subject_id, uuid.UUID):
            raise TypeError("capability subject_id must be a UUID")
        message = (
            _ROOT_CONTEXT
            + bytes((len(self._context),))
            + self._context
            + subject_id.bytes
        )
        signature = hmac.digest(self._secret, message, "sha256")
        return base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")

    def verify(self, subject_id: uuid.UUID, token: str) -> bool:
        """Verify a presented capability without exposing comparison timing."""
        if not isinstance(token, str) or not token:
            return False
        return hmac.compare_digest(self.issue(subject_id), token)

    @staticmethod
    def digest(token: str) -> str:
        """Return a database lookup digest for a presented bearer token."""
        if not isinstance(token, str) or not token:
            raise ValueError("capability token must not be blank")
        return hashlib.sha256(token.encode("utf-8")).hexdigest()


__all__ = ["CapabilityTokenCodec"]
