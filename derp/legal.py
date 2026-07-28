"""Stable public legal-document coordinates used by Telegram controls."""

from typing import Final


class TermsAcceptanceRequiredError(RuntimeError):
    """Public purchase intent creation requires the current Terms version."""


LEGAL_DOCUMENT_VERSION: Final = "2026-07-28"
TERMS_ACCEPTANCE_VERSION: Final = "derp-terms-v0.1.0"
PRIVACY_POLICY_URL: Final = "https://github.com/uburuntu/derp/blob/v0.1.0/PRIVACY.md"
TERMS_OF_USE_URL: Final = "https://github.com/uburuntu/derp/blob/v0.1.0/TERMS.md"

__all__ = [
    "LEGAL_DOCUMENT_VERSION",
    "PRIVACY_POLICY_URL",
    "TERMS_ACCEPTANCE_VERSION",
    "TermsAcceptanceRequiredError",
    "TERMS_OF_USE_URL",
]
