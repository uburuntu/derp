"""Release gates for Stars intake independent from reconciliation."""

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class CommercePolicy:
    """Control creation of public invoices without blocking captured payments."""

    public_intake_enabled: bool = False


CLOSED_COMMERCE_POLICY: Final = CommercePolicy()

__all__ = ["CLOSED_COMMERCE_POLICY", "CommercePolicy"]
