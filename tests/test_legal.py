"""Public legal documents remain reachable from the in-bot privacy surface."""

from pathlib import Path

from derp.legal import (
    LEGAL_DOCUMENT_VERSION,
    TERMS_ACCEPTANCE_VERSION,
)

ROOT = Path(__file__).parents[1]


def test_public_legal_documents_are_bilingual_and_versioned() -> None:
    privacy = (ROOT / "PRIVACY.md").read_text()
    terms = (ROOT / "TERMS.md").read_text()

    assert LEGAL_DOCUMENT_VERSION == "2026-08-03"
    assert TERMS_ACCEPTANCE_VERSION == "derp-terms-20260803"
    for document in (privacy, terms):
        assert "3 August 2026" in document
        assert "3 августа 2026" in document
        assert "## English" in document
        assert "## Русский" in document
        assert "`/privacy`" in document
        assert "`/support`" in document
        assert "`/paysupport`" not in document


def test_privacy_policy_discloses_external_processors_and_retention_limits() -> None:
    privacy = (ROOT / "PRIVACY.md").read_text()

    for processor in ("Telegram", "OpenRouter", "Google", "Logfire"):
        assert processor in privacy
    assert "7, 30, or 90-day" in privacy
    assert "at most 6 hours" in privacy
    assert "no automated expiry" in privacy
    assert "автоматического срока удаления" in privacy
