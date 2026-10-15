"""Protect credentials at logging and diagnostic boundaries."""

import json
import logging
import re
from collections.abc import Iterable
from urllib.parse import quote, quote_plus, urlsplit

MASK = "[REDACTED]"
_SENSITIVE_KEY = re.compile(
    r"token|password|secret|authorization|cookie|api.?key|credential|dsn", re.IGNORECASE
)
_PATTERNS = (
    re.compile(r"\b(?:bot)?\d{6,12}:[A-Za-z0-9_-]{30,}\b"),  # Telegram bot tokens (also inside file URLs)
    re.compile(r"\bsk-(?:or-)?[A-Za-z0-9_-]{16,}\b"),  # OpenRouter / OpenAI keys
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{25,})\b"),
    re.compile(r"(?<=://)[^\s/@]+:[^\s/@]+@"),  # URL credentials
    re.compile(r"(?i)((?:authorization|password|api_key|access_token)\s*[:=]\s*)[^\s,;}]+"),
)


class Redactor:
    """Masks known secret values (and their encodings) plus secret-shaped strings."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        variants: set[str] = set()
        for secret in secrets:
            if not secret:
                continue
            candidates = [secret]
            if "://" in secret:
                try:
                    if password := urlsplit(secret).password:
                        candidates.append(password)
                except ValueError:
                    pass
            for value in candidates:
                variants.update(
                    (
                        value,
                        quote(value, safe=""),
                        quote_plus(value),
                        json.dumps(value)[1:-1],
                        repr(value)[1:-1],
                    )
                )
        self._variants = sorted((v for v in variants if len(v) >= 6), key=len, reverse=True)

    def __call__(self, value: object) -> str:
        text = str(value)
        for secret in self._variants:
            text = text.replace(secret, MASK)
        for pattern in _PATTERNS:
            text = pattern.sub(MASK, text)
        return text

    def json(self, value: object) -> object:
        """Redact diagnostic JSON without touching keys or numeric IDs."""
        if isinstance(value, dict):
            return {
                key: MASK if _SENSITIVE_KEY.search(str(key)) else self.json(item)
                for key, item in value.items()
            }
        if isinstance(value, list | tuple):
            return [self.json(item) for item in value]
        return self(value) if isinstance(value, str) else value


class RedactingFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor, fmt: str | None = None) -> None:
        super().__init__(fmt)
        self.redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self.redactor(super().format(record))
