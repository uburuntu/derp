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
from opentelemetry.trace import Link, SpanContext, Status, StatusCode, TraceFlags
from pydantic_ai import Agent
from pydantic_ai.capabilities import Instrumentation
from pydantic_ai.models.instrumented import InstrumentationSettings
from pydantic_ai.models.test import TestModel

from derp.observability import (
    Observability,
    ObservabilityConfig,
    PrivacySafeLogfireLoggingHandler,
    RedactingTracerProvider,
    configure_observability,
    enable_early_pydantic_instrumentation,
    redact_exception_callback,
    report_exception,
    scrubbing_options,
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
    monkeypatch.setenv(
        "OTEL_GOOGLE_GENAI_GENERATE_CONTENT_CONFIG_INCLUDES",
        "*",
    )
    client = MagicMock(spec=logfire.Logfire)
    tracer_provider = MagicMock()
    meter_provider = MagicMock()
    client.config.get_tracer_provider.return_value = tracer_provider
    client.config.get_meter_provider.return_value = meter_provider

    def enable_events_like_upstream(**_: object) -> None:
        os.environ["OTEL_INSTRUMENTATION_GENAI_EMIT_EVENT"] = "true"

    client.instrument_google_genai.side_effect = enable_events_like_upstream
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
    pydantic_ai_tracer_provider = pydantic_ai_kwargs["tracer_provider"]
    assert isinstance(pydantic_ai_tracer_provider, RedactingTracerProvider)
    assert pydantic_ai_tracer_provider.capture_ai_text is False
    assert pydantic_ai_kwargs["meter_provider"] is meter_provider
    google_genai_kwargs = client.instrument_google_genai.call_args.kwargs
    google_genai_tracer_provider = google_genai_kwargs["tracer_provider"]
    assert isinstance(google_genai_tracer_provider, RedactingTracerProvider)
    assert google_genai_tracer_provider is not pydantic_ai_tracer_provider
    assert google_genai_tracer_provider.capture_ai_text is False
    assert google_genai_kwargs["meter_provider"] is meter_provider
    client.instrument_google_genai.assert_called_once_with(
        tracer_provider=google_genai_tracer_provider,
        meter_provider=meter_provider,
    )
    client.instrument_system_metrics.assert_called_once_with(base="basic")
    assert os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == (
        "NO_CONTENT"
    )
    assert os.environ["OTEL_INSTRUMENTATION_GENAI_EMIT_EVENT"] == "false"
    assert "OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK" not in os.environ
    assert "OTEL_GOOGLE_GENAI_GENERATE_CONTENT_CONFIG_INCLUDES" not in os.environ
    assert os.environ["OTEL_GOOGLE_GENAI_GENERATE_CONTENT_CONFIG_EXCLUDES"] == "*"


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
    assert (
        client.instrument_pydantic_ai.call_args.kwargs[
            "tracer_provider"
        ].capture_ai_text
        is True
    )
    assert (
        client.instrument_google_genai.call_args.kwargs[
            "tracer_provider"
        ].capture_ai_text
        is False
    )
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

    def fail_after_enabling_events(**_: object) -> None:
        os.environ["OTEL_INSTRUMENTATION_GENAI_EMIT_EVENT"] = "true"
        raise RuntimeError("instrumentation failed")

    client.instrument_google_genai.side_effect = fail_after_enabling_events
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
    assert os.environ["OTEL_INSTRUMENTATION_GENAI_EMIT_EVENT"] == "false"


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


def test_stdlib_bridge_redacts_embedded_urls(
    capfire: CaptureLogfire,
) -> None:
    private_value = "PRIVATE-SIGNED-URL-SENTINEL"
    signed_url = f"https://files.example.test/download/{private_value}?signature=secret"
    logger = logging.getLogger("test.private_url")
    original_handlers = logger.handlers[:]
    original_level = logger.level
    original_propagate = logger.propagate
    logger.handlers = [PrivacySafeLogfireLoggingHandler()]
    logger.setLevel(logging.INFO)
    logger.propagate = False

    try:
        logger.info("download_failed url=%s", signed_url)
        logfire.DEFAULT_LOGFIRE_INSTANCE.force_flush()
    finally:
        logger.handlers = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate

    spans = capfire.exporter.exported_spans_as_dict()
    span = next(span for span in spans if span["name"].startswith("download_failed"))
    serialized = json.dumps(span)
    assert private_value not in serialized
    assert signed_url not in serialized
    assert "[URL redacted]" in serialized


def test_scrubbing_redacts_pydantic_failure_details_and_urls() -> None:
    client = logfire.configure(
        local=True,
        send_to_logfire=False,
        console=False,
        scrubbing=scrubbing_options(),
    )
    private_message = "PRIVATE-CUSTOM-VALIDATOR-MESSAGE"
    private_input = "PRIVATE-VALIDATION-INPUT"
    private_url = "https://files.example.test/private?signature=PRIVATE-SIGNATURE"

    try:
        errors, _ = client.config.scrubber.scrub_value(
            ("attributes", "errors"),
            [
                {
                    "msg": private_message,
                    "input": private_input,
                    "ctx": {"error": private_message},
                }
            ],
        )
        scrubbed, _ = client.config.scrubber.scrub_value(
            ("attributes",),
            {"source_url": private_url},
        )
    finally:
        client.shutdown()

    serialized = json.dumps({"errors": errors, **scrubbed})
    assert private_message not in serialized
    assert private_input not in serialized
    assert private_url not in serialized
    assert "Scrubbed" in serialized


def test_content_scrubbing_preserves_logfire_and_stdlib_messages() -> None:
    exporter = TestExporter()
    client = logfire.configure(
        local=True,
        send_to_logfire=False,
        console=False,
        scrubbing=scrubbing_options(),
        additional_span_processors=[SimpleSpanProcessor(exporter)],
    )
    logger = logging.getLogger("test.benign_message")
    original_handlers = logger.handlers[:]
    original_level = logger.level
    original_propagate = logger.propagate
    logger.handlers = [PrivacySafeLogfireLoggingHandler(logfire_instance=client)]
    logger.setLevel(logging.INFO)
    logger.propagate = False

    try:
        client.info("benign_structured_event", outcome="success")
        logger.info("benign stdlib event: %s", "success")
        client.force_flush()
    finally:
        logger.handlers = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate
        client.shutdown()

    spans = exporter.exported_spans_as_dict()
    direct = next(span for span in spans if span["name"] == "benign_structured_event")
    stdlib = next(span for span in spans if span["name"] == "benign stdlib event: %s")
    assert direct["attributes"]["logfire.msg"] == "benign_structured_event"
    assert direct["attributes"]["logfire.msg_template"] == "benign_structured_event"
    assert stdlib["attributes"]["logfire.msg"] == "benign stdlib event: success"
    assert stdlib["attributes"]["logfire.msg_template"] == "benign stdlib event: %s"


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


def test_redacting_tracer_removes_urls_and_ai_content() -> None:
    exporter = TestExporter()
    provider = SDKTracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = RedactingTracerProvider(provider).get_tracer("test.privacy")
    private_text = "PRIVATE-PROMPT-SENTINEL"
    private_args = "PRIVATE-TOOL-ARGUMENTS"
    private_url = "https://files.example.test/private?signature=secret"
    messages = json.dumps(
        [
            {
                "role": "user",
                "parts": [
                    {"type": "text", "content": private_text},
                    {"type": "uri", "uri": private_url},
                    {"type": "tool_call", "arguments": private_args},
                ],
            }
        ]
    )

    span = tracer.start_span(
        "agent.run",
        attributes={
            "gen_ai.input.messages": messages,
            "gen_ai.tool.call.arguments": private_args,
            "http.target": f"/download?token={private_args}",
            "url.full": private_url,
        },
        links=[
            Link(
                SpanContext(
                    trace_id=1,
                    span_id=1,
                    is_remote=False,
                    trace_flags=TraceFlags(1),
                ),
                {"url.full": private_url},
            )
        ],
    )
    span.end()
    provider.shutdown()

    exported = exporter.exported_spans_as_dict()
    serialized = json.dumps(exported)
    assert private_text not in serialized
    assert private_args not in serialized
    assert private_url not in serialized
    attributes = exported[0]["attributes"]
    assert json.loads(attributes["gen_ai.input.messages"]) == [
        {
            "role": "user",
            "parts": [
                {"type": "text"},
                {"type": "uri"},
                {"type": "tool_call"},
            ],
        }
    ]
    assert attributes["gen_ai.tool.call.arguments"] == "[AI content redacted]"
    assert attributes["http.target"] == "[URL redacted]"
    assert attributes["url.full"] == "[URL redacted]"
    assert exported[0]["links"][0]["attributes"]["url.full"] == "[URL redacted]"


def test_local_ai_capture_keeps_text_but_removes_non_text_content() -> None:
    exporter = TestExporter()
    provider = SDKTracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = RedactingTracerProvider(
        provider,
        capture_ai_text=True,
    ).get_tracer("test.local-ai")
    safe_text = "LOCAL-TEXT-CAPTURE-SENTINEL"
    private_args = "PRIVATE-TOOL-ARGUMENTS"
    private_result = "PRIVATE-TOOL-RESULT"
    private_url = "https://files.example.test/private?signature=secret"
    messages = json.dumps(
        [
            {
                "role": "user",
                "parts": [
                    {
                        "type": "text",
                        "content": f"{safe_text} {private_url}",
                    },
                    {"type": "uri", "uri": private_url},
                    {
                        "type": "tool_call",
                        "name": "lookup",
                        "arguments": private_args,
                    },
                    {
                        "type": "tool_call_response",
                        "name": "lookup",
                        "result": private_result,
                    },
                ],
            }
        ]
    )

    span = tracer.start_span(
        "agent.run",
        attributes={
            "gen_ai.input.messages": messages,
            "gen_ai.tool.call.arguments": private_args,
            "gen_ai.tool.call.result": private_result,
        },
    )
    span.end()
    provider.shutdown()

    serialized = json.dumps(exporter.exported_spans_as_dict())
    assert safe_text in serialized
    assert private_args not in serialized
    assert private_result not in serialized
    assert private_url not in serialized
    assert "[URL redacted]" in serialized


def test_pydantic_ai_v5_emits_content_free_aggregated_usage() -> None:
    exporter = TestExporter()
    provider = SDKTracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    settings = InstrumentationSettings(
        tracer_provider=RedactingTracerProvider(provider),
        include_content=False,
        include_binary_content=False,
        include_model_request_parameters=False,
        version=5,
    )
    agent = Agent(
        TestModel(),
        name="privacy_test",
        capabilities=[Instrumentation(settings=settings)],
    )
    private_prompt = "PRIVATE-PYDANTIC-AI-PROMPT"

    agent.run_sync(private_prompt)
    provider.shutdown()

    spans = exporter.exported_spans_as_dict()
    serialized = json.dumps(spans)
    assert private_prompt not in serialized
    run_span = next(
        span for span in spans if span["name"] == "invoke_agent privacy_test"
    )
    model_span = next(span for span in spans if span["name"] == "chat test")
    assert run_span["attributes"]["gen_ai.aggregated_usage.input_tokens"] > 0
    assert run_span["attributes"]["gen_ai.aggregated_usage.output_tokens"] > 0
    assert "gen_ai.usage.input_tokens" not in run_span["attributes"]
    assert model_span["attributes"]["gen_ai.usage.input_tokens"] > 0
    all_messages = json.loads(run_span["attributes"]["pydantic_ai.all_messages"])
    assert all(
        "content" not in part for message in all_messages for part in message["parts"]
    )


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
