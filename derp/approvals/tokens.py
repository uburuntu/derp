"""Opaque capabilities for deferred paid-tool decisions."""

from __future__ import annotations

import uuid
from typing import Final

from derp.security import CapabilityTokenCodec

_APPROVAL_CONTEXT: Final = b"deferred-tool-approval:v1"


class ApprovalTokenCodec:
    """Issue capabilities that cannot be reused in another token domain."""

    def __init__(self, secret: bytes) -> None:
        self._codec = CapabilityTokenCodec(secret, context=_APPROVAL_CONTEXT)

    def issue(self, request_id: uuid.UUID) -> str:
        return self._codec.issue(request_id)

    def verify(self, request_id: uuid.UUID, token: str) -> bool:
        return self._codec.verify(request_id, token)

    @staticmethod
    def digest(token: str) -> str:
        return CapabilityTokenCodec.digest(token)


__all__ = ["ApprovalTokenCodec"]
