"""Application observability configuration and lifecycle ownership."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import traceback
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from hashlib import sha256
from pathlib import Path
from typing import Literal

import logfire
from logfire.types import ExceptionCallbackHelper
from opentelemetry.context import Context
from opentelemetry.metrics import MeterProvider
from opentelemetry.trace import (
    Link,
    Span,
    SpanContext,
    SpanKind,
    Status,
    StatusCode,
    Tracer,
    TracerProvider,
)
from opentelemetry.util.types import Attributes, AttributeValue

_CONTENT_FIELD_PATTERN = (
    r"^(?:input|text|caption|prompt|query|content|response|message|memory|"
    r"args|kwargs|msg|ctx|invoice_payload|payload|tool_arguments|tool_response)$"
)
_REDACT_FIELD_PATTERN = (
    r"^(?:telegram_bot_token|google_api_paid_key|logfire_token|"
    r"telegram_charge_id|provider_charge_id|charge_id)$"
)
_REDACT_VALUE_PATTERN = r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"
_URL_FIELD_PATTERN = r"(?:^|[._ -])(?:url|uri)(?:$|[._ -])"
_URL_VALUE_PATTERN = r"\b[a-z][a-z0-9+.-]*://[^\s<>\"']+"
_URL_RE = re.compile(_URL_VALUE_PATTERN, re.IGNORECASE)
_GENAI_CAPTURE_ENV = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
_GENAI_EVENTS_ENV = "OTEL_INSTRUMENTATION_GENAI_EMIT_EVENT"
_GENAI_COMPLETION_HOOK_ENV = "OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK"
_GENAI_CONFIG_INCLUDES_ENV = "OTEL_GOOGLE_GENAI_GENERATE_CONTENT_CONFIG_INCLUDES"
_GENAI_CONFIG_EXCLUDES_ENV = "OTEL_GOOGLE_GENAI_GENERATE_CONTENT_CONFIG_EXCLUDES"
_REDACTED_EXCEPTION_MESSAGE = "Exception details redacted"
_REDACTED_AI_CONTENT = "[AI content redacted]"
_REDACTED_URL = "[URL redacted]"

_AI_MESSAGE_ATTRIBUTES = frozenset(
    {
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.system_instructions",
        "pydantic_ai.all_messages",
    }
)
_AI_PRIVATE_ATTRIBUTES = frozenset(
    {
        "event_body",
        "final_result",
        "gen_ai.agent.description",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
        "metadata",
        "pydantic_ai.tool.deferral.metadata",
        "tool_arguments",
        "tool_response",
    }
)
_AI_MESSAGE_SAFE_KEYS = frozenset(
    {
        "builtin",
        "finish_reason",
        "id",
        "mime_type",
        "modality",
        "name",
        "parts",
        "role",
        "type",
    }
)
_AI_TEXT_PART_TYPES = frozenset({"text", "thinking"})
_SENSITIVE_URL_ATTRIBUTES = frozenset({"http.target"})


class PrivacySafeLogfireLoggingHandler(logfire.LogfireLoggingHandler):
    """Bridge stdlib logs without exporting exception messages in arguments."""

    def emit(self, record: logging.LogRecord) -> None:
        super().emit(_redact_log_record(record))


class RedactingSpan(Span):
    """Delegate span operations while removing exception and status content."""

    def __init__(self, delegate: Span, *, capture_ai_text: bool = False) -> None:
        self._delegate = delegate
        self._capture_ai_text = capture_ai_text

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    def end(self, end_time: int | None = None) -> None:
        self._delegate.end(end_time)

    def get_span_context(self) -> SpanContext:
        return self._delegate.get_span_context()

    def set_attributes(self, attributes: Mapping[str, AttributeValue]) -> None:
        self._delegate.set_attributes(
            _redact_span_attributes(
                attributes,
                capture_ai_text=self._capture_ai_text,
            )
        )

    def set_attribute(self, key: str, value: AttributeValue) -> None:
        self._delegate.set_attribute(
            key,
            _redact_span_attribute(
                key,
                value,
                capture_ai_text=self._capture_ai_text,
            ),
        )

    def add_event(
        self,
        name: str,
        attributes: Attributes = None,
        timestamp: int | None = None,
    ) -> None:
        if name == "exception":
            attributes = _redact_exception_attributes(attributes)
        if attributes:
            attributes = _redact_span_attributes(
                attributes,
                capture_ai_text=self._capture_ai_text,
            )
        self._delegate.add_event(name, attributes, timestamp)

    def add_link(self, context: SpanContext, attributes: Attributes = None) -> None:
        if attributes:
            attributes = _redact_span_attributes(
                attributes,
                capture_ai_text=self._capture_ai_text,
            )
        self._delegate.add_link(context, attributes)

    def update_name(self, name: str) -> None:
        self._delegate.update_name(name)

    def is_recording(self) -> bool:
        return self._delegate.is_recording()

    def set_status(
        self,
        status: Status | StatusCode,
        description: str | None = None,
    ) -> None:
        status_code = status.status_code if isinstance(status, Status) else status
        if status_code is StatusCode.ERROR:
            self._delegate.set_status(
                Status(StatusCode.ERROR, f"Error: {_REDACTED_EXCEPTION_MESSAGE}")
            )
            return
        self._delegate.set_status(status, description)

    def record_exception(
        self,
        exception: BaseException,
        attributes: Attributes = None,
        timestamp: int | None = None,
        escaped: bool = False,
    ) -> None:
        safe_attributes = dict(attributes or {})
        safe_attributes["exception.type"] = _safe_error_type(
            type(exception).__qualname__, type(exception).__name__
        )
        self._delegate.record_exception(
            redacted_exception(exception),
            attributes=_redact_span_attributes(
                _redact_exception_attributes(safe_attributes) or {},
                capture_ai_text=self._capture_ai_text,
            ),
            timestamp=timestamp,
            escaped=escaped,
        )


class RedactingTracer(Tracer):
    """Wrap every integration span in a privacy-safe span."""

    def __init__(self, delegate: Tracer, *, capture_ai_text: bool = False) -> None:
        self._delegate = delegate
        self._capture_ai_text = capture_ai_text

    def start_span(
        self,
        name: str,
        context: Context | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        start_time: int | None = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
    ) -> Span:
        span = self._delegate.start_span(
            name,
            context=context,
            kind=kind,
            attributes=(
                _redact_span_attributes(
                    attributes,
                    capture_ai_text=self._capture_ai_text,
                )
                if attributes
                else None
            ),
            links=_redact_links(links, capture_ai_text=self._capture_ai_text),
            start_time=start_time,
            record_exception=record_exception,
            set_status_on_exception=set_status_on_exception,
        )
        return RedactingSpan(span, capture_ai_text=self._capture_ai_text)

    @contextmanager
    def start_as_current_span(
        self,
        name: str,
        context: Context | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: Attributes = None,
        links: Sequence[Link] | None = None,
        start_time: int | None = None,
        record_exception: bool = True,
        set_status_on_exception: bool = True,
        end_on_exit: bool = True,
    ) -> Iterator[Span]:
        with self._delegate.start_as_current_span(
            name,
            context=context,
            kind=kind,
            attributes=(
                _redact_span_attributes(
                    attributes,
                    capture_ai_text=self._capture_ai_text,
                )
                if attributes
                else None
            ),
            links=_redact_links(links, capture_ai_text=self._capture_ai_text),
            start_time=start_time,
            record_exception=False,
            set_status_on_exception=False,
            end_on_exit=end_on_exit,
        ) as span:
            safe_span = RedactingSpan(
                span,
                capture_ai_text=self._capture_ai_text,
            )
            try:
                yield safe_span
            except BaseException as exc:
                if record_exception:
                    safe_span.record_exception(exc, escaped=True)
                if set_status_on_exception:
                    safe_span.set_status(StatusCode.ERROR)
                raise


class RedactingTracerProvider(TracerProvider):
    """Provide redacting tracers over an existing configured provider."""

    def __init__(
        self,
        delegate: TracerProvider,
        *,
        capture_ai_text: bool = False,
    ) -> None:
        self._delegate = delegate
        self.capture_ai_text = capture_ai_text

    def get_tracer(
        self,
        instrumenting_module_name: str,
        instrumenting_library_version: str | None = None,
        schema_url: str | None = None,
        attributes: Attributes = None,
    ) -> Tracer:
        return RedactingTracer(
            self._delegate.get_tracer(
                instrumenting_module_name,
                instrumenting_library_version,
                schema_url,
                attributes,
            ),
            capture_ai_text=self.capture_ai_text,
        )


@dataclass(frozen=True, slots=True)
class ObservabilityConfig:
    """Primitive inputs needed to configure observability."""

    service_name: str
    service_version: str
    environment: str
    token: str
    capture_ai_content: bool = False


@dataclass(frozen=True, slots=True)
class Observability:
    """Own the configured Logfire instance through process shutdown."""

    logfire: logfire.Logfire

    def shutdown(self, timeout_millis: int = 10_000) -> bool:
        """Flush telemetry and release exporter resources."""
        return self.logfire.shutdown(timeout_millis=timeout_millis, flush=True)


def shutdown_observability(
    observability: Observability,
    timeout_millis: int = 10_000,
) -> bool | None:
    """Shut down telemetry without masking an application failure."""
    try:
        return observability.shutdown(timeout_millis=timeout_millis)
    except Exception as exc:
        sys.stderr.write(f"Telemetry shutdown failed: {type(exc).__name__}\n")
        return None


@cache
def enable_early_pydantic_instrumentation() -> None:
    """Install the Pydantic plugin before application models are imported."""
    logfire.instrument_pydantic(record="failure")


def scrubbing_options() -> logfire.ScrubbingOptions:
    """Return defense-in-depth redaction rules for application telemetry."""
    return logfire.ScrubbingOptions(
        extra_patterns=(
            _CONTENT_FIELD_PATTERN,
            _REDACT_FIELD_PATTERN,
            _REDACT_VALUE_PATTERN,
            _URL_FIELD_PATTERN,
            _URL_VALUE_PATTERN,
        )
    )


def telemetry_fingerprint(value: str) -> str:
    """Return a non-reversible correlation key for a high-entropy identifier."""
    return sha256(value.encode()).hexdigest()[:16]


def report_exception(
    message: str,
    *,
    exception: BaseException | None = None,
    level: Literal["warning", "error"] = "error",
    logfire_instance: logfire.Logfire | None = None,
    **attributes: object,
) -> None:
    """Report an exception without exporting its potentially sensitive message."""
    if exception is None:
        exception = sys.exc_info()[1]
    if exception is None:
        raise RuntimeError("report_exception requires an active exception")

    client = logfire_instance or logfire.DEFAULT_LOGFIRE_INSTANCE
    safe_exception = redacted_exception(exception)
    attributes = {"error.type": type(exception).__name__, **attributes}
    exc_info = (type(safe_exception), safe_exception, safe_exception.__traceback__)
    if level == "warning":
        client.warning(message, _exc_info=exc_info, **attributes)
    else:
        client.error(message, _exc_info=exc_info, **attributes)


def redacted_exception(exception: BaseException) -> RuntimeError:
    """Retain traceback frames while discarding exception text and chaining."""
    safe_exception = RuntimeError(_REDACTED_EXCEPTION_MESSAGE).with_traceback(
        exception.__traceback__
    )
    safe_exception.__cause__ = None
    safe_exception.__context__ = None
    safe_exception.__suppress_context__ = True
    return safe_exception


def _redact_log_record(record: logging.LogRecord) -> logging.LogRecord:
    values = {
        key: (_REDACTED_URL if _is_url_attribute(key) else _redact_logging_value(value))
        for key, value in record.__dict__.items()
    }
    if record.exc_info and (exception := record.exc_info[1]) is not None:
        safe_exception = redacted_exception(exception)
        values["exc_info"] = (
            type(safe_exception),
            safe_exception,
            record.exc_info[2],
        )
        values["error.type"] = _safe_error_type(
            type(exception).__qualname__, type(exception).__name__
        )
    values["exc_text"] = None
    return logging.makeLogRecord(values)


def _redact_logging_value(value: object) -> object:
    if isinstance(value, BaseException):
        return _REDACTED_EXCEPTION_MESSAGE
    if isinstance(value, str):
        return _redact_urls(value)
    if type(value) is tuple:
        return tuple(_redact_logging_value(item) for item in value)
    if type(value) is list:
        return [_redact_logging_value(item) for item in value]
    if type(value) is dict:
        return {
            key: (
                _REDACTED_URL
                if isinstance(key, str) and _is_url_attribute(key)
                else _redact_logging_value(item)
            )
            for key, item in value.items()
        }
    return value


def _redact_span_attributes(
    attributes: Mapping[str, AttributeValue],
    *,
    capture_ai_text: bool = False,
) -> dict[str, AttributeValue]:
    return {
        key: _redact_span_attribute(
            key,
            value,
            capture_ai_text=capture_ai_text,
        )
        for key, value in attributes.items()
    }


def _redact_links(
    links: Sequence[Link] | None,
    *,
    capture_ai_text: bool,
) -> Sequence[Link] | None:
    if links is None:
        return None
    return [
        Link(
            link.context,
            _redact_span_attributes(
                link.attributes or {},
                capture_ai_text=capture_ai_text,
            ),
        )
        for link in links
    ]


def _redact_span_attribute(
    key: str,
    value: AttributeValue,
    *,
    capture_ai_text: bool = False,
) -> AttributeValue:
    if key in {"error.message", "exception.message"}:
        return _REDACTED_EXCEPTION_MESSAGE
    if key == "exception.stacktrace":
        return f"Error: {_REDACTED_EXCEPTION_MESSAGE}"
    if key in _AI_MESSAGE_ATTRIBUTES:
        return _sanitize_ai_messages(value, include_text=capture_ai_text)
    if key in _AI_PRIVATE_ATTRIBUTES:
        return _REDACTED_AI_CONTENT
    if _is_url_attribute(key):
        return _REDACTED_URL
    if isinstance(value, str):
        return _redact_urls(value)
    if isinstance(value, tuple | list):
        redacted = tuple(
            _redact_urls(item) if isinstance(item, str) else item for item in value
        )
        return redacted if isinstance(value, tuple) else list(redacted)
    return value


def _sanitize_ai_messages(value: AttributeValue, *, include_text: bool) -> str:
    if not isinstance(value, str):
        return _REDACTED_AI_CONTENT
    try:
        messages = json.loads(value)
    except TypeError, ValueError:
        return _REDACTED_AI_CONTENT
    return json.dumps(
        _sanitize_ai_node(messages, include_text=include_text),
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _sanitize_ai_node(value: object, *, include_text: bool) -> object:
    if isinstance(value, list):
        return [_sanitize_ai_node(item, include_text=include_text) for item in value]
    if not isinstance(value, dict):
        return value if isinstance(value, bool | int | float) else None

    part_type = value.get("type")
    safe: dict[str, object] = {}
    for key, item in value.items():
        if key in _AI_MESSAGE_SAFE_KEYS:
            safe[key] = (
                _sanitize_ai_node(item, include_text=include_text)
                if isinstance(item, list | dict)
                else _redact_urls(item)
                if isinstance(item, str)
                else item
            )
        elif (
            key == "content"
            and include_text
            and part_type in _AI_TEXT_PART_TYPES
            and isinstance(item, str)
        ):
            safe[key] = _redact_urls(item)
    return safe


def _is_url_attribute(key: str) -> bool:
    return key.lower() in _SENSITIVE_URL_ATTRIBUTES or bool(
        re.search(_URL_FIELD_PATTERN, key, re.IGNORECASE)
    )


def _redact_urls(value: str) -> str:
    return _URL_RE.sub(_REDACTED_URL, value)


def _redact_exception_attributes(attributes: Attributes) -> Attributes:
    safe_attributes = dict(attributes or {})
    error_type = _safe_error_type(safe_attributes.get("exception.type"), "Error")
    safe_attributes.update(
        {
            "exception.message": _REDACTED_EXCEPTION_MESSAGE,
            "exception.stacktrace": (f"{error_type}: {_REDACTED_EXCEPTION_MESSAGE}"),
        }
    )
    return safe_attributes


def _safe_error_type(value: object, fallback: str) -> str:
    if not isinstance(value, str) or len(value) > 200:
        return fallback
    if not value.replace(".", "").replace("_", "").isalnum():
        return fallback
    return value


def redact_exception_callback(helper: ExceptionCallbackHelper) -> None:
    """Remove exception content from every Logfire and integration span."""
    span_attributes = helper.span.attributes or {}
    error_type = _safe_error_type(
        span_attributes.get("error.type")
        or helper.event_attributes.get("exception.type"),
        type(helper.exception).__name__,
    )
    frames = traceback.extract_tb(helper.exception.__traceback__)
    safe_stacktrace = "\n".join(
        f'  File "{Path(frame.filename).name}", line {frame.lineno}, in {frame.name}'
        for frame in frames
    )
    if safe_stacktrace:
        safe_stacktrace += "\n"
    safe_stacktrace += f"{error_type}: {_REDACTED_EXCEPTION_MESSAGE}"

    helper.event_attributes.update(
        {
            "exception.type": error_type,
            "exception.message": _REDACTED_EXCEPTION_MESSAGE,
            "exception.stacktrace": safe_stacktrace,
        }
    )
    helper.span.set_attribute("error.type", error_type)
    if helper.create_issue:
        helper.issue_fingerprint_source = f"{error_type}\n{safe_stacktrace}"
    if helper.span.status.status_code is StatusCode.ERROR:
        helper.span.set_status(
            Status(StatusCode.ERROR, f"{error_type}: {_REDACTED_EXCEPTION_MESSAGE}")
        )


def configure_observability(config: ObservabilityConfig) -> Observability:
    """Configure Logfire and all process-wide integrations exactly once."""
    level = "debug" if config.environment == "dev" else "info"
    capture_ai_content = config.environment == "dev" and config.capture_ai_content
    client = logfire.configure(
        token=config.token,
        send_to_logfire="if-token-present",
        service_name=config.service_name,
        service_version=config.service_version,
        environment=config.environment,
        console=logfire.ConsoleOptions(
            span_style="simple",
            min_log_level=level,
            show_project_link=config.environment == "dev",
        ),
        metrics=logfire.MetricsOptions(collect_in_spans=True),
        scrubbing=scrubbing_options(),
        inspect_arguments=False,
        min_level=level,
        add_baggage_to_attributes=False,
        distributed_tracing=False,
        advanced=logfire.AdvancedOptions(exception_callback=redact_exception_callback),
    )

    try:
        tracer_provider = client.config.get_tracer_provider()
        meter_provider = client.config.get_meter_provider()
        pydantic_ai_tracer_provider = RedactingTracerProvider(
            tracer_provider,
            capture_ai_text=capture_ai_content,
        )
        google_genai_tracer_provider = RedactingTracerProvider(
            tracer_provider,
        )
        _configure_logging(client, config.environment)
        client.instrument_pydantic_ai(
            version=5,
            include_content=capture_ai_content,
            include_binary_content=False,
            include_model_request_parameters=False,
            tracer_provider=pydantic_ai_tracer_provider,
            meter_provider=meter_provider,
        )
        _instrument_google_genai(
            client,
            tracer_provider=google_genai_tracer_provider,
            meter_provider=meter_provider,
        )
        client.instrument_system_metrics(base="basic")
    except Exception:
        shutdown_observability(Observability(client))
        raise

    return Observability(logfire=client)


def _instrument_google_genai(
    client: logfire.Logfire,
    *,
    tracer_provider: TracerProvider,
    meter_provider: MeterProvider,
) -> None:
    _disable_google_genai_content_capture()
    try:
        client.instrument_google_genai(
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
        )
    finally:
        # opentelemetry-instrumentation-google-genai 1.0b1 enables events while
        # installing its wrappers, so restore the process-wide privacy policy.
        _disable_google_genai_content_capture()


def _disable_google_genai_content_capture() -> None:
    os.environ[_GENAI_CAPTURE_ENV] = "NO_CONTENT"
    os.environ[_GENAI_EVENTS_ENV] = "false"
    os.environ.pop(_GENAI_COMPLETION_HOOK_ENV, None)
    os.environ.pop(_GENAI_CONFIG_INCLUDES_ENV, None)
    os.environ[_GENAI_CONFIG_EXCLUDES_ENV] = "*"


def _configure_logging(client: logfire.Logfire, environment: str) -> None:
    app_level = logging.DEBUG if environment == "dev" else logging.INFO
    fallback = logging.StreamHandler()
    fallback.setFormatter(
        logging.Formatter(
            "[%(asctime)s][%(name)s][%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handler = PrivacySafeLogfireLoggingHandler(
        level=app_level,
        fallback=fallback,
        logfire_instance=client,
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("derp").setLevel(app_level)

    for logger_name in (
        "google_genai",
        "httpcore",
        "httpx",
        "sqlalchemy.engine",
    ):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
