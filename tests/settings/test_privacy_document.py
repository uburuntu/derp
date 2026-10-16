"""PRIVACY.md (the URL BotFather links to) is the /privacy page, word for word."""

import re
from pathlib import Path

from derp.locales import t

ROOT = Path(__file__).resolve().parents[2]


def document() -> str:
    parts = [
        "<!-- Generated from derp/locales (pages.privacy); tests/settings/test_privacy_document.py keeps it in sync. -->"
    ]
    for locale in ("en", "ru"):
        page = t(locale, "pages.privacy").strip()
        page = re.sub(r"<details><summary>(.+?)</summary>", r"### \1", page)
        page = page.replace("</details>", "").replace("🔒 **", "## ").replace("**\n", "\n", 1)
        parts.append(re.sub(r"\n{3,}", "\n\n", page).strip())
    return "\n\n".join(parts) + "\n"


def test_privacy_document_matches_the_privacy_page() -> None:
    assert (ROOT / "PRIVACY.md").read_text() == document(), (
        "regenerate PRIVACY.md from tests/settings/test_privacy_document.py"
    )
