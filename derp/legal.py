"""Stable public legal-document coordinates used by Telegram controls."""

from typing import Final


class TermsAcceptanceRequiredError(RuntimeError):
    """Public purchase intent creation requires the current Terms version."""


LEGAL_DOCUMENT_VERSION: Final = "2026-08-03"
TERMS_ACCEPTANCE_VERSION: Final = "derp-terms-20260803"

__all__ = [
    "LEGAL_DOCUMENT_VERSION",
    "TERMS_ACCEPTANCE_VERSION",
    "TermsAcceptanceRequiredError",
]
