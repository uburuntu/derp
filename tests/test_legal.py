"""Public legal documents remain reachable from the in-bot privacy surface."""

from pathlib import Path

from derp.legal import LEGAL_DOCUMENT_VERSION, PRIVACY_POLICY_URL, TERMS_OF_USE_URL

ROOT = Path(__file__).parents[1]


def test_public_legal_documents_are_bilingual_and_versioned() -> None:
    privacy = (ROOT / "PRIVACY.md").read_text()
    terms = (ROOT / "TERMS.md").read_text()

    assert LEGAL_DOCUMENT_VERSION == "2026-07-28"
    assert PRIVACY_POLICY_URL.endswith("/blob/main/PRIVACY.md")
    assert TERMS_OF_USE_URL.endswith("/blob/main/TERMS.md")
    for document in (privacy, terms):
        assert "28 July 2026" in document
        assert "28 июля 2026" in document
        assert "## English" in document
        assert "## Русский" in document
        assert "`/privacy`" in document


def test_privacy_policy_discloses_external_processors_and_retention_limits() -> None:
    privacy = (ROOT / "PRIVACY.md").read_text()

    for processor in ("Telegram", "OpenRouter", "Google", "Logfire"):
        assert processor in privacy
    assert "7, 30, or 90-day" in privacy
    assert "at most 6 hours" in privacy
    assert "no automated expiry" in privacy
    assert "автоматического срока удаления" in privacy
