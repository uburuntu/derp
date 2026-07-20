"""Compact Telegram callback contracts for durable delivery recovery."""

from __future__ import annotations

from typing import Annotated

from aiogram.filters.callback_data import CallbackData
from pydantic import Field


class DeliveryResendCallback(CallbackData, prefix="ir"):
    """Opaque recovery capability; all authority is resolved server-side."""

    token: Annotated[
        str,
        Field(min_length=1, max_length=60, pattern=r"^[A-Za-z0-9_-]+$"),
    ]


__all__ = ["DeliveryResendCallback"]
