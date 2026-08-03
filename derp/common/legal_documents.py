"""Load and paginate the bilingual legal documents for Telegram."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Final


class LegalDocumentKind(StrEnum):
    """Legal documents exposed by the in-bot reader."""

    TERMS = "terms"
    PRIVACY = "privacy"


@dataclass(frozen=True, slots=True)
class LegalDocumentPage:
    """One bounded, localized page of a versioned legal document."""

    document: LegalDocumentKind
    title: str
    version: str
    body: str
    number: int
    total: int


_PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
_PAGE_BODY_LIMIT: Final = 3_200
_DOCUMENT_PATHS: Final = {
    LegalDocumentKind.TERMS: _PROJECT_ROOT / "TERMS.md",
    LegalDocumentKind.PRIVACY: _PROJECT_ROOT / "PRIVACY.md",
}
_DOCUMENT_TITLES: Final = {
    (LegalDocumentKind.TERMS, "en"): "Terms of use",
    (LegalDocumentKind.TERMS, "ru"): "Условия использования",
    (LegalDocumentKind.PRIVACY, "en"): "Privacy policy",
    (LegalDocumentKind.PRIVACY, "ru"): "Политика конфиденциальности",
}
_MARKDOWN_LINK = re.compile(r"\[([^]]+)]\(([^)]+)\)")


@lru_cache(maxsize=4)
def legal_document_pages(
    document: LegalDocumentKind,
    locale: str,
) -> tuple[LegalDocumentPage, ...]:
    """Return complete localized content split below Telegram's message limit."""
    language = "ru" if locale.lower().startswith("ru") else "en"
    source = _DOCUMENT_PATHS[document].read_text(encoding="utf-8")
    version, body = _localized_document(source, language)
    chunks = _paginate(_plain_text(body))
    title = _DOCUMENT_TITLES[document, language]
    return tuple(
        LegalDocumentPage(
            document=document,
            title=title,
            version=version,
            body=chunk,
            number=index + 1,
            total=len(chunks),
        )
        for index, chunk in enumerate(chunks)
    )


def _localized_document(source: str, language: str) -> tuple[str, str]:
    english_marker = "\n## English\n"
    russian_marker = "\n## Русский\n"
    before_english, separator, after_english = source.partition(english_marker)
    if not separator:
        raise ValueError("legal document has no English section")
    english, separator, russian = after_english.partition(russian_marker)
    if not separator:
        raise ValueError("legal document has no Russian section")

    version_lines = [
        line.strip() for line in before_english.splitlines() if line.strip()
    ]
    if len(version_lines) < 3:
        raise ValueError("legal document has no bilingual version header")
    version = version_lines[2] if language == "ru" else version_lines[1]
    return version, russian.strip() if language == "ru" else english.strip()


def _plain_text(markdown: str) -> str:
    def replace_link(match: re.Match[str]) -> str:
        label, target = match.groups()
        if target in {"PRIVACY.md", "TERMS.md"}:
            return label
        return f"{label}: {target}"

    text = _MARKDOWN_LINK.sub(replace_link, markdown)
    lines: list[str] = []
    for line in text.splitlines():
        line = re.sub(r"^#{1,6}\s+", "", line)
        lines.append(line.replace("`", ""))
    return "\n".join(lines).strip()


def _paginate(text: str) -> tuple[str, ...]:
    paragraphs = text.split("\n\n")
    pages: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= _PAGE_BODY_LIMIT:
            current = candidate
            continue
        if current:
            pages.append(current)
        if len(paragraph) <= _PAGE_BODY_LIMIT:
            current = paragraph
            continue
        lines = paragraph.splitlines()
        current = ""
        for line in lines:
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) > _PAGE_BODY_LIMIT and current:
                pages.append(current)
                current = line
            else:
                current = candidate
    if current:
        pages.append(current)
    if not pages:
        raise ValueError("legal document is empty")
    return tuple(pages)


__all__ = ["LegalDocumentKind", "LegalDocumentPage", "legal_document_pages"]
