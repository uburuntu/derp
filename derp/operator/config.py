"""Non-secret configuration exposed to operator controls."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

_SAFE_LABEL = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass(frozen=True, slots=True, init=False)
class OperatorControlConfig:
    """Validated runtime flags needed by the private operator console."""

    environment: str
    service_version: str
    public_purchases_enabled: bool
    ai_content_capture_enabled: bool
    operator_ids: frozenset[int] = field(repr=False)

    def __init__(
        self,
        *,
        environment: str,
        service_version: str,
        public_purchases_enabled: bool,
        ai_content_capture_enabled: bool,
        operator_ids: Iterable[int],
    ) -> None:
        if _SAFE_LABEL.fullmatch(environment) is None:
            raise ValueError("environment must be a safe non-empty label")
        if _SAFE_LABEL.fullmatch(service_version) is None:
            raise ValueError("service_version must be a safe non-empty label")
        if not isinstance(public_purchases_enabled, bool):
            raise TypeError("public_purchases_enabled must be a bool")
        if not isinstance(ai_content_capture_enabled, bool):
            raise TypeError("ai_content_capture_enabled must be a bool")
        ids = frozenset(operator_ids)
        if any(
            isinstance(operator_id, bool)
            or not isinstance(operator_id, int)
            or operator_id <= 0
            for operator_id in ids
        ):
            raise ValueError("operator IDs must be positive integers")
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "service_version", service_version)
        object.__setattr__(
            self,
            "public_purchases_enabled",
            public_purchases_enabled,
        )
        object.__setattr__(
            self,
            "ai_content_capture_enabled",
            ai_content_capture_enabled,
        )
        object.__setattr__(self, "operator_ids", ids)


__all__ = ["OperatorControlConfig"]
