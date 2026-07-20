"""Tests for the application observability subsystem."""

from __future__ import annotations

import json
import logging
import os
from unittest.mock import MagicMock, patch

import logfire
import pytest
from aiogram.loggers import event as aiogram_event_logger
from logfire.testing import CaptureLogfire, TestExporter
from opentelemetry.sdk.trace import TracerProvider as SDKTracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import Status, StatusCode

from derp.observability import (
    Observability,
    ObservabilityConfig,
    PrivacySafeLogfireLoggingHandler,
    RedactingTracerProvider,
    configure_observability,
    enable_early_pydantic_instrumentation,
    redact_exception_callback,
    report_exception,
    telemetry_fingerprint,
)

TEST_CREDENTIAL = "not-a-real-credential"


def test_configures_privacy_safe_integrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "SPAN_AND_EVENT"
    )
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_EMIT_EVENT", "true")
    monkeypatch.setenv("OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK", "upload")
    client = MagicMock(spec=logfire.Logfire)
    config = ObservabilityConfig(
        service_name="derp",
        service_version="1.2.3",
        environment="prod",
        token=TEST_CREDENTIAL,
    )

    with (
        patch("derp.observability.logfire.configure", return_value=client) as configure,
        patch("derp.observability._configure_logging") as configure_logging,
    ):
        observability = configure_observability(config)

    assert observability.logfire is client
    kwargs = configure.call_args.kwargs
    assert kwargs["token"] == TEST_CREDENTIAL
    assert kwargs["send_to_logfire"] == "if-token-present"
    assert kwargs["service_name"] == "derp"
    assert kwargs["service_version"] == "1.2.3"
    assert kwargs["environment"] == "prod"
    assert kwargs["inspect_arguments"] is False
    assert kwargs["add_baggage_to_attributes"] is False
    assert kwargs["distributed_tracing"] is False
    assert kwargs["metrics"].collect_in_spans is True
    assert kwargs["scrubbing"].extra_patterns
    assert kwargs["advanced"].exception_callback is redact_exception_callback
    configure_logging.assert_called_once_with(client, "prod")
    pydantic_ai_kwargs = client.instrument_pydantic_ai.call_args.kwargs
    assert pydantic_ai_kwargs["version"] == 5
    assert pydantic_ai_kwargs["include_content"] is False
    assert pydantic_ai_kwargs["include_binary_content"] is False
    assert pydantic_ai_kwargs["include_model_request_parameters"] is False
    tracer_provider = pydantic_ai_kwargs["tracer_provider"]
    assert isinstance(tracer_provider, RedactingTracerProvider)
    client.instrument_google_genai.assert_called_once_with(
        tracer_provider=tracer_provider
    )
    client.instrument_system_metrics.assert_called_once_with(base="basic")
    assert os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == (
        "NO_CONTENT"
    )
    assert os.environ["OTEL_INSTRUMENTATION_GENAI_EMIT_EVENT"] == "false"
    assert "OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK" not in os.environ


def test_ai_content_capture_requires_explicit_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", raising=False
    )
    client = MagicMock(spec=logfire.Logfire)
    config = ObservabilityConfig(
        service_name="derp",
        service_version="1.2.3",
        environment="dev",
        token=TEST_CREDENTIAL,
        capture_ai_content=True,
    )

    with (
        patch("derp.observability.logfire.configure", return_value=client),
        patch("derp.observability._configure_logging"),
    ):
        configure_observability(config)

    assert client.instrument_pydantic_ai.call_args.kwargs["include_content"] is True
    assert os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == (
        "NO_CONTENT"
    )


def test_production_cannot_enable_ai_content() -> None:
    client = MagicMock(spec=logfire.Logfire)
    config = ObservabilityConfig(
        service_name="derp",
        service_version="1.2.3",
        environment="prod",
        token=TEST_CREDENTIAL,
        capture_ai_content=True,
    )

    with (
        patch("derp.observability.logfire.configure", return_value=client),
        patch("derp.observability._configure_logging"),
    ):
        configure_observability(config)

    assert client.instrument_pydantic_ai.call_args.kwargs["include_content"] is False


def test_integration_failure_shuts_down_configured_client() -> None:
    client = MagicMock(spec=logfire.Logfire)
    client.instrument_google_genai.side_effect = RuntimeError("instrumentation failed")
    config = ObservabilityConfig(
        service_name="derp",
        service_version="1.2.3",
        environment="prod",
        token=TEST_CREDENTIAL,
    )

    with (
        patch("derp.observability.logfire.configure", return_value=client),
        patch("derp.observability._configure_logging"),
        pytest.raises(RuntimeError, match="instrumentation failed"),
    ):
        configure_observability(config)

    client.shutdown.assert_called_once_with(timeout_millis=10_000, flush=True)


def test_startup_failure_is_preserved_when_shutdown_also_fails(capsys) -> None:
    client = MagicMock(spec=logfire.Logfire)
    client.instrument_google_genai.side_effect = RuntimeError("instrumentation failed")
    client.shutdown.side_effect = RuntimeError("private-shutdown-detail")
    config = ObservabilityConfig(
        service_name="derp",
        service_version="1.2.3",
        environment="prod",
        token=TEST_CREDENTIAL,
    )

    with (
        patch("derp.observability.logfire.configure", return_value=client),
        patch("derp.observability._configure_logging"),
        pytest.raises(RuntimeError, match="instrumentation failed"),
    ):
        configure_observability(config)

    stderr = capsys.readouterr().err
    assert "Telemetry shutdown failed: RuntimeError" in stderr
    assert "private-shutdown-detail" not in stderr


def test_observability_owns_shutdown() -> None:
    client = MagicMock(spec=logfire.Logfire)
    client.shutdown.return_value = True

    result = Observability(client).shutdown(timeout_millis=123)

    assert result is True
    client.shutdown.assert_called_once_with(timeout_millis=123, flush=True)


def test_early_pydantic_hook_is_idempotent() -> None:
    enable_early_pydantic_instrumentation.cache_clear()
    try:
        with patch("derp.observability.logfire.instrument_pydantic") as instrument:
            enable_early_pydantic_instrumentation()
            enable_early_pydantic_instrumentation()
        instrument.assert_called_once_with(record="failure")
    finally:
        enable_early_pydantic_instrumentation.cache_clear()


def test_telemetry_fingerprint_is_stable_without_exposing_input() -> None:
    value = "high-entropy-provider-identifier"

    fingerprint = telemetry_fingerprint(value)

    assert fingerprint == telemetry_fingerprint(value)
    assert len(fingerprint) == 16
    assert value not in fingerprint


def test_report_exception_redacts_message_and_preserves_error_type(
    capfire: CaptureLogfire,
) -> None:
    private_value = "private-exception-value-sentinel"

    try:
        raise RuntimeError(private_value)
    except RuntimeError as exc:
        report_exception("operation_failed", exception=exc)

    spans = capfire.exporter.exported_spans_as_dict()
    span = next(span for span in spans if span["name"] == "operation_failed")
    serialized = str(span)
    assert private_value not in serialized
    assert span["attributes"]["error.type"] == "RuntimeError"
    assert "Exception details redacted" in serialized


def test_stdlib_bridge_redacts_aiogram_exception_arguments(
    capfire: CaptureLogfire,
) -> None:
    private_value = "PRIVATE-AIOGRAM-EXCEPTION-SENTINEL"
    original_handlers = aiogram_event_logger.handlers[:]
    original_level = aiogram_event_logger.level
    original_propagate = aiogram_event_logger.propagate
    aiogram_event_logger.handlers = [PrivacySafeLogfireLoggingHandler()]
    aiogram_event_logger.setLevel(logging.ERROR)
    aiogram_event_logger.propagate = False

    try:
        try:
            raise ValueError(private_value)
        except ValueError as exc:
            aiogram_event_logger.exception(
                "Cause exception while process update id=%d by bot id=%d\n%s: %s",
                1,
                2,
                type(exc).__name__,
                exc,
            )
        logfire.DEFAULT_LOGFIRE_INSTANCE.force_flush()
    finally:
        aiogram_event_logger.handlers = original_handlers
        aiogram_event_logger.setLevel(original_level)
        aiogram_event_logger.propagate = original_propagate

    spans = capfire.exporter.exported_spans_as_dict()
    serialized = json.dumps(spans)
    assert private_value not in serialized
    span = next(span for span in spans if span["name"].startswith("Cause exception"))
    assert span["attributes"]["error.type"] == "ValueError"
    assert "Exception details redacted" in span["attributes"]["logfire.msg"]


def test_global_exception_callback_redacts_instrumented_spans() -> None:
    exporter = TestExporter()
    client = logfire.configure(
        local=True,
        send_to_logfire=False,
        console=False,
        additional_span_processors=[SimpleSpanProcessor(exporter)],
        advanced=logfire.AdvancedOptions(exception_callback=redact_exception_callback),
    )
    private_value = "private-provider-response-sentinel"

    try:
        with pytest.raises(RuntimeError, match=private_value):
            with client.span("agent.run"):
                raise RuntimeError(private_value)
        client.force_flush()
    finally:
        client.shutdown()

    spans = exporter.exported_spans_as_dict()
    serialized = json.dumps(spans)
    assert private_value not in serialized
    span = next(span for span in spans if span["name"] == "agent.run")
    event = next(event for event in span["events"] if event["name"] == "exception")
    assert event["attributes"]["exception.message"] == "Exception details redacted"
    assert event["attributes"]["exception.type"] == "RuntimeError"
    raw_span = next(
        span for span in reversed(exporter.exported_spans) if span.name == "agent.run"
    )
    assert raw_span.status.description == "RuntimeError: Exception details redacted"


def test_redacting_tracer_provider_sanitizes_third_party_status() -> None:
    exporter = TestExporter()
    provider = SDKTracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = RedactingTracerProvider(provider).get_tracer("test.integration")
    private_value = "private-provider-status-sentinel"

    try:
        with pytest.raises(RuntimeError, match=private_value):
            with tracer.start_as_current_span(
                "provider.request",
                attributes={"gen_ai.response.model": "model-x"},
            ) as span:
                assert span.attributes["gen_ai.response.model"] == "model-x"
                raise RuntimeError(private_value)
    finally:
        provider.shutdown()

    spans = exporter.exported_spans_as_dict()
    assert private_value not in json.dumps(spans)
    span = next(span for span in spans if span["name"] == "provider.request")
    event = next(event for event in span["events"] if event["name"] == "exception")
    assert event["attributes"]["exception.message"] == "Exception details redacted"
    assert event["attributes"]["exception.type"] == "RuntimeError"
    raw_span = next(
        span
        for span in reversed(exporter.exported_spans)
        if span.name == "provider.request"
    )
    assert raw_span.status.description == "Error: Exception details redacted"


def test_redacting_tracer_sanitizes_manual_provider_errors() -> None:
    exporter = TestExporter()
    provider = SDKTracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = RedactingTracerProvider(provider).get_tracer("test.google")
    private_value = "private-google-error-sentinel"

    span = tracer.start_span("generate_content")
    span.set_attribute("error.message", private_value)
    span.set_status(Status(StatusCode.ERROR, private_value))
    span.end()
    provider.shutdown()

    spans = exporter.exported_spans_as_dict()
    assert private_value not in json.dumps(spans)
    raw_span = next(
        span
        for span in reversed(exporter.exported_spans)
        if span.name == "generate_content"
    )
    assert raw_span.status.description == "Error: Exception details redacted"
    assert raw_span.attributes["error.message"] == "Exception details redacted"


def test_global_exception_callback_preserves_warning_status() -> None:
    exporter = TestExporter()
    client = logfire.configure(
        local=True,
        send_to_logfire=False,
        console=False,
        additional_span_processors=[SimpleSpanProcessor(exporter)],
        advanced=logfire.AdvancedOptions(exception_callback=redact_exception_callback),
    )
    private_value = "private-warning-sentinel"

    try:
        try:
            raise RuntimeError(private_value)
        except RuntimeError as exc:
            client.warning(
                "fallback_used",
                _exc_info=(type(exc), exc, exc.__traceback__),
            )
        client.force_flush()
    finally:
        client.shutdown()

    spans = exporter.exported_spans_as_dict()
    assert private_value not in json.dumps(spans)
    raw_span = next(
        span
        for span in reversed(exporter.exported_spans)
        if span.name == "fallback_used"
    )
    assert raw_span.status.status_code is StatusCode.UNSET
