"""User-facing copy in en + ru. Catalogs are flat TOML tables; keys use dots for sections.

``t(locale, "start.greeting", name=...)`` formats with ``str.format``; unknown
locales fall back to English. ``tests/locales`` keeps keys and placeholders in sync.
"""

import tomllib
from functools import cache
from pathlib import Path
from string import Formatter
from typing import Any

LOCALES = ("en", "ru")
DEFAULT = "en"
_DIR = Path(__file__).parent


def _flatten(table: dict[str, Any], prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in table.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{name}."))
        else:
            flat[name] = str(value)
    return flat


@cache
def catalog(locale: str) -> dict[str, str]:
    return _flatten(tomllib.loads((_DIR / f"{locale}.toml").read_text("utf-8")))


def resolve(language_code: str | None) -> str:
    """Map a Telegram ``language_code`` (or a stored chat locale) to a supported locale."""
    code = (language_code or "").lower().split("-")[0]
    return code if code in LOCALES else DEFAULT


def t(locale: str | None, key: str, /, **values: object) -> str:
    text = catalog(resolve(locale)).get(key) or catalog(DEFAULT)[key]
    return text.format(**values) if values else text


def placeholders(text: str) -> set[str]:
    return {field for _, field, _, _ in Formatter().parse(text) if field}
