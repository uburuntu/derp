"""Durable checks for production translation catalogs."""

from pathlib import Path

from babel.messages.catalog import Catalog, Message
from babel.messages.pofile import read_po

RUSSIAN_CATALOG = Path("derp/locales/ru/LC_MESSAGES/messages.po")
ENGLISH_CATALOG = Path("derp/locales/en/LC_MESSAGES/messages.po")


def _read_catalog(path: Path, *, locale: str) -> Catalog:
    with path.open(encoding="utf-8") as file:
        return read_po(file, locale=locale, ignore_obsolete=True, abort_invalid=True)


def _translations(message: Message) -> tuple[str, ...]:
    if isinstance(message.string, tuple):
        return message.string
    return (message.string or "",)


def test_russian_catalog_is_complete_and_reviewed() -> None:
    catalog = _read_catalog(RUSSIAN_CATALOG, locale="ru")
    messages = [message for message in catalog if message.id]

    untranslated = [
        message.id for message in messages if not all(_translations(message))
    ]
    fuzzy = [message.id for message in messages if message.fuzzy]

    assert untranslated == []
    assert fuzzy == []


def test_english_catalog_has_no_fuzzy_fallbacks() -> None:
    catalog = _read_catalog(ENGLISH_CATALOG, locale="en")

    assert [message.id for message in catalog if message.id and message.fuzzy] == []


def test_russian_catalog_preserves_formats_and_persona_name() -> None:
    catalog = _read_catalog(RUSSIAN_CATALOG, locale="ru")
    messages = [message for message in catalog if message.id]

    format_errors = {
        message.id: [str(error) for error in errors]
        for message in messages
        if (errors := message.check(catalog))
    }
    latin_persona = [
        message.id
        for message in messages
        if any("Derp" in translation for translation in _translations(message))
    ]

    assert format_errors == {}
    assert latin_persona == []
