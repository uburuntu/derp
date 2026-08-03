"""Canonical rendering for topic-scoped shared facts as untrusted model data."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ApprovedFact:
    """Minimum approved-fact projection visible to the model."""

    id: UUID
    text: str

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("Approved fact text must not be blank")


def render_approved_facts(facts: Sequence[ApprovedFact]) -> str:
    """Render deterministic data that explicitly carries no instruction authority."""
    return json.dumps(
        {
            "facts": [{"id": str(fact.id), "text": fact.text} for fact in facts],
            "type": "untrusted_approved_shared_facts",
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


__all__ = ["ApprovedFact", "render_approved_facts"]
