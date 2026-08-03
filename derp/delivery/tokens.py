"""Stable opaque capabilities for authenticated delivery resends."""

from __future__ import annotations

import uuid
from typing import Final

from derp.security import CapabilityTokenCodec

_RESEND_CONTEXT: Final = b"delivery-resend:v1"


class ResendTokenCodec:
    """Issue restart-stable HMAC capabilities and database-safe digests."""

    def __init__(self, secret: bytes) -> None:
        if not isinstance(secret, bytes):
            raise TypeError("resend token secret must be bytes")
        if len(secret) < 32:
            raise ValueError("resend token secret must contain at least 32 bytes")
        self._codec = CapabilityTokenCodec(secret, context=_RESEND_CONTEXT)

    def issue(self, intent_id: uuid.UUID) -> str:
        """Derive one opaque callback-safe capability from an intent identity."""
        if not isinstance(intent_id, uuid.UUID):
            raise TypeError("intent_id must be a UUID")
        return self._codec.issue(intent_id)

    @staticmethod
    def digest(token: str) -> str:
        """Return the one-way database lookup digest for a presented token."""
        if not isinstance(token, str) or not token:
            raise ValueError("resend token must not be blank")
        return CapabilityTokenCodec.digest(token)


__all__ = ["ResendTokenCodec"]
