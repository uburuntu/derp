"""Logging and Logfire, owned by this module alone.

Content-free by default (numeric IDs, kinds, counts, durations, outcomes). The
only content export is a redacted, truncated exchange when an agent run fails.
Pydantic AI is deliberately not instrumented at all (its spans record exception
messages and prompts), so the only span is the content-free ``telegram.update``
around each update; agent runs add no spans of their own (pre-run decisions log one content-free
event: probabilities, outcome, duration). Logfire is optional and fails
soft; HTTPX is never instrumented globally because Telegram file URLs embed the
bot token.
"""

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import logfire
from aiogram.types import Update

from derp.config import Settings
from derp.ops.redaction import RedactingFormatter, Redactor

log = logging.getLogger("derp")
_redactor = Redactor()
EXCHANGE_LIMIT = 4_000


def configure(settings: Settings) -> Redactor:
    global _redactor
    _redactor = Redactor(settings.secret_values())
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter(_redactor, "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    for noisy in ("aiogram.event", "httpx", "openai", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        logfire.configure(
            send_to_logfire="if-token-present",
            token=settings.logfire_token or None,
            service_name="derp",
            environment=settings.environment,
            console=False,
            inspect_arguments=False,
            metrics=False,
        )
    except Exception as error:  # noqa: BLE001 - telemetry must never stop the bot
        log.warning("telemetry disabled: %s", type(error).__name__)
    return _redactor


def redact(value: object) -> str:
    return _redactor(value)


def update_attributes(update: Update) -> dict[str, Any]:
    """Numeric identifiers and kinds only — never text, captions or file IDs."""
    attributes: dict[str, Any] = {"update_id": update.update_id, "update_type": update.event_type}
    event = update.event
    chat = getattr(event, "chat", None) or getattr(getattr(event, "message", None), "chat", None)
    user = getattr(event, "from_user", None) or getattr(event, "user", None)
    if chat is not None:
        attributes["chat_id"] = chat.id
        attributes["chat_type"] = chat.type
    if user is not None:
        attributes["user_id"] = user.id
    if isinstance(message_id := getattr(event, "message_id", None), int):
        attributes["message_id"] = message_id
    if isinstance(thread_id := getattr(event, "message_thread_id", None), int):
        attributes["thread_id"] = thread_id
    return attributes


@contextmanager
def update_span(update: Update) -> Iterator[None]:
    """Span per update. Exceptions are recorded by type only: Logfire would otherwise
    export the exception message and traceback, which can carry content."""
    error: Exception | None = None
    with logfire.span("telegram.update", **update_attributes(update)) as span:
        try:
            yield
        except Exception as exc:  # noqa: BLE001 - re-raised below, outside the span
            error = exc
            span.set_attribute("error_type", type(exc).__name__)
            span.set_level("error")
    if error is not None:
        raise error


def record_run_failure(run_id: int, error: BaseException, exchange: str) -> None:
    """The single sanctioned content export: a redacted exchange of a failed run."""
    logfire.error(
        "agent.run.failed",
        run_id=run_id,
        error_type=type(error).__name__,
        exchange=_redactor(exchange)[:EXCHANGE_LIMIT],
    )
    log.error("agent run %s failed: %s", run_id, type(error).__name__)


def record_decision(chat_id: int, outcome: str, duration_ms: int, **answers: float | str) -> None:
    """One call to the decision pin: its outcome, duration and answers as probabilities or labels."""
    attributes: dict[str, Any] = {
        "chat_id": chat_id,
        "outcome": outcome,
        "duration_ms": duration_ms,
        **answers,
    }
    logfire.info("agent.decision", **attributes)
    log.info(
        "decision in %s: %s in %d ms %s",
        chat_id,
        outcome,
        duration_ms,
        " ".join(f"{name}={value}" for name, value in answers.items()),
    )
