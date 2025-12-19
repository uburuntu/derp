"""Credit pack definitions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CreditPack:
    """A purchasable credit pack."""

    id: str
    name: str
    stars: int  # Telegram Stars cost
    credits: int  # Credits received
    bonus_pct: int  # Bonus percentage for display


CREDIT_PACKS: dict[str, CreditPack] = {
    "starter": CreditPack("starter", "Starter", 50, 50, 0),
    "basic": CreditPack("basic", "Basic", 150, 165, 10),
    "standard": CreditPack("standard", "Standard", 500, 600, 20),
    "bulk": CreditPack("bulk", "Bulk", 1500, 2000, 33),
}
