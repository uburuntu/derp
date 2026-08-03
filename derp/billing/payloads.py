"""Opaque Telegram payload creation and one-way persistence identity."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from typing import Final

PAYLOAD_PREFIX: Final = "dpi1_"
DEFAULT_TOKEN_BYTES: Final = 24


def generate_invoice_payload(
    token_factory: Callable[[], str] | None = None,
) -> str:
    """Return an opaque payload within Telegram's 128-byte hard limit."""
    token = (
        token_factory()
        if token_factory is not None
        else secrets.token_urlsafe(DEFAULT_TOKEN_BYTES)
    )
    if (
        not token
        or not token.isascii()
        or any(character.isspace() for character in token)
    ):
        raise ValueError(
            "invoice payload tokens must be non-empty ASCII without spaces"
        )
    payload = f"{PAYLOAD_PREFIX}{token}"
    if len(payload.encode("utf-8")) > 128:
        raise ValueError("invoice payload exceeds Telegram's 128-byte limit")
    return payload


def hash_invoice_payload(payload: str) -> str:
    """Hash the bearer payload so database reads cannot recreate invoice links."""
    if not payload:
        raise ValueError("invoice payload must not be empty")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "DEFAULT_TOKEN_BYTES",
    "PAYLOAD_PREFIX",
    "generate_invoice_payload",
    "hash_invoice_payload",
]
