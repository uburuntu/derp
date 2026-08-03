# Observability

`derp/observability.py` is the only owner of process-wide Logfire and
OpenTelemetry configuration. Bootstrap enables failure-only Pydantic validation
instrumentation before application models are imported, configures Logfire once,
passes the configured client into the runtime, and synchronously flushes and
shuts down traces, logs, and metrics after the application exits.

## Provider ownership

Logfire owns the SDK tracer, meter, and logger providers. Pydantic AI v5 and the
Google GenAI SDK receive Logfire's meter provider and privacy-wrapped views of
its tracer provider. This keeps every integration on one resource and export
pipeline while redacting data before third-party spans reach that pipeline.

The configured integrations are:

- Pydantic AI instrumentation version 5 with aggregated run-level usage
- content-free Google GenAI child spans for direct SDK and provider calls
- failure-only Pydantic validation records
- basic system metrics and metrics aggregated within spans
- a privacy-safe standard-library logging bridge

## Content policy

Production never exports prompts, completions, tool arguments or results,
binary data, model request parameters, provider responses, or completion-hook
payloads. Google GenAI content capture remains disabled in every environment.
Arbitrary Google request-config attributes are also disabled because the
upstream instrumentor can enable them through process environment variables.

`LOGFIRE_CAPTURE_AI_CONTENT=true` is honored only with `ENVIRONMENT=dev`. It
allows Pydantic AI text and thinking parts while the tracer boundary still
removes tool arguments and results, structured run metadata, file identifiers,
binary data, and media URIs. URLs embedded in retained text are redacted.

Do not globally instrument HTTPX or aiohttp. Telegram download URLs contain the
bot token, and global client instrumentation treats full URL fields as safe.
Owned HTTP clients should use explicit content-free spans instead.

SQLAlchemy and asyncpg auto-instrumentation also remain off. Their statement and
parameter capture can expose application values, and it would duplicate the
explicit operation-level database spans already owned by Derp. Those spans keep
only operation names, bounded counts, timing, and outcomes; SQLAlchemy's stdlib
logger remains at `WARNING`.

## Errors and logs

`report_exception()` is the recovery-boundary API. It retains safe traceback
locations and the exception type while replacing exception messages, source
text, chains, and status descriptions. The global exception callback applies
the same policy to auto-instrumented spans.

The logging bridge redacts exception arguments and URL-like values before
formatting. The root logger stays at `INFO`; only the `derp` namespace is raised
to `DEBUG` in development. Provider, HTTP, and SQLAlchemy loggers remain at
`WARNING` to prevent SDK debug payloads from bypassing instrumentation settings.
Pydantic validation error inputs, custom validator messages, and contexts are
scrubbed before export.

## Upstream boundaries

Logfire deliberately treats OpenTelemetry URL fields and GenAI message fields
as scrubber-safe. Derp therefore enforces URL and AI-content policy in the
integration tracer wrapper instead of relying on regex scrubbing alone.

The Google GenAI OpenTelemetry instrumentor currently enables event emission
while installing its wrappers and supports an environment-controlled config
allowlist. Derp restores `NO_CONTENT`, disables events and completion hooks, and
forces the config allowlist closed both before and after installation. Focused
tests make those workarounds explicit so a future dependency upgrade cannot
silently weaken the policy.

Run `uv run pytest -q tests/test_observability.py` to verify provider wiring,
Pydantic AI v5 usage attributes, content and URL redaction, exception handling,
and shutdown ownership.
