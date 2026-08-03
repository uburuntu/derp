"""Keyed, content-free identities for immutable operation requests."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from derp.execution import Feature

MIN_REQUEST_BINDING_KEY_BYTES = 32
MAX_REQUEST_BINDING_PAYLOAD_BYTES = 128 * 1024
_REQUEST_BINDING_DOMAIN = b"derp:operation-request-binding:v1\0"


@dataclass(frozen=True, slots=True)
class OperationRequestBinder:
    """Create non-reversible HMAC identities from validated request values."""

    _key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self._key, bytes):
            raise TypeError("request binding key must be immutable bytes")
        if len(self._key) < MIN_REQUEST_BINDING_KEY_BYTES:
            raise ValueError(
                f"request binding key must contain at least "
                f"{MIN_REQUEST_BINDING_KEY_BYTES} bytes"
            )

    def bind(
        self,
        feature: Feature,
        values: Mapping[str, object],
    ) -> str:
        """Bind one feature's validated JSON-compatible request without storing it."""
        if not isinstance(feature, Feature):
            raise TypeError("request binding feature must be a Feature")
        if not isinstance(values, Mapping):
            raise TypeError("request binding values must be a mapping")
        try:
            payload = json.dumps(
                dict(values),
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        except TypeError, ValueError:
            raise ValueError(
                "request binding values must be finite JSON-compatible data"
            ) from None
        if len(payload) > MAX_REQUEST_BINDING_PAYLOAD_BYTES:
            raise ValueError("request binding payload exceeds its bounded size")
        message = (
            _REQUEST_BINDING_DOMAIN + feature.value.encode("ascii") + b"\0" + payload
        )
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()


__all__ = [
    "MAX_REQUEST_BINDING_PAYLOAD_BYTES",
    "MIN_REQUEST_BINDING_KEY_BYTES",
    "OperationRequestBinder",
]
