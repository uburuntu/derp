"""Stable opaque capabilities for authenticated delivery resends."""

from __future__ import annotations

import base64
import hashlib
import hmac
import uuid
from typing import Final

_RESEND_CONTEXT: Final = b"derp:delivery-resend:v1:"
_MINIMUM_SECRET_BYTES: Final = 32


class ResendTokenCodec:
    """Issue restart-stable HMAC capabilities and database-safe digests."""

    def __init__(self, secret: bytes) -> None:
        if not isinstance(secret, bytes):
            raise TypeError("resend token secret must be bytes")
        if len(secret) < _MINIMUM_SECRET_BYTES:
            raise ValueError("resend token secret must contain at least 32 bytes")
        self._secret = secret

    def issue(self, intent_id: uuid.UUID) -> str:
        """Derive one opaque callback-safe capability from an intent identity."""
        if not isinstance(intent_id, uuid.UUID):
            raise TypeError("intent_id must be a UUID")
        signature = hmac.digest(
            self._secret,
            _RESEND_CONTEXT + intent_id.bytes,
            "sha256",
        )
        return base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")

    @staticmethod
    def digest(token: str) -> str:
        """Return the one-way database lookup digest for a presented token."""
        if not isinstance(token, str) or not token:
            raise ValueError("resend token must not be blank")
        return hashlib.sha256(token.encode()).hexdigest()


__all__ = ["ResendTokenCodec"]
