/** Central observability module — structured logging, tracing, and metrics via Logfire + OTEL */

import {
    metrics as otelMetrics,
    type Span,
    SpanStatusCode,
    trace,
} from "@opentelemetry/api";
import logfire from "@pydantic/logfire-node";
import type { Config } from "../config";

// ── Initialization ──────────────────────────────────────────────────────────

let initialized = false;
let redactionValues: string[] = [];
const handledFailureSpans = new WeakSet<Span>();

export function initObservability(cfg: Config): void {
    if (initialized) return;
    redactionValues = [
        cfg.telegramBotToken,
        cfg.databaseUrl,
        cfg.googleApiKey,
        cfg.googleApiPaidKey,
        cfg.openrouterApiKey,
        cfg.logfireToken,
        cfg.braveSearchApiKey,
        ...cfg.googleApiKeys,
    ].filter((value): value is string => Boolean(value));

    logfire.configure({
        token: cfg.logfireToken,
        serviceName: cfg.otelServiceName,
        serviceVersion: "1.0.0",
    });

    initMetrics();
    initialized = true;
}

export async function shutdownObservability(): Promise<void> {
    // logfire-node registers process exit handlers that flush spans/metrics.
    // Explicit shutdown is handled by the OTEL SDK internals.
}

// ── Logger ──────────────────────────────────────────────────────────────────

export function redactString(value: string): string {
    let redacted = value;
    for (const secret of redactionValues) {
        redacted = redacted.replaceAll(secret, "[redacted]");
    }
    return redacted
        .replace(/bot\d+:[A-Za-z0-9_-]+/g, "bot[redacted]")
        .replace(/postgres(?:ql)?:\/\/\S+/g, "postgres://[redacted]");
}

function redactValue(value: unknown): unknown {
    if (typeof value === "string") return redactString(value);
    if (Array.isArray(value)) return value.map(redactValue);
    if (value && typeof value === "object") {
        return Object.fromEntries(
            Object.entries(value as Record<string, unknown>).map(
                ([key, nested]) => [key, redactValue(nested)],
            ),
        );
    }
    return value;
}

export function redactErrorMessage(error: unknown): string {
    return redactString(error instanceof Error ? error.message : String(error));
}

export function redactedException(error: unknown): Error {
    const redacted = new Error(redactErrorMessage(error));
    if (error instanceof Error) {
        redacted.name = error.name;
    }
    return redacted;
}

function redactAttrs(
    attrs: Record<string, unknown> | undefined,
): Record<string, unknown> | undefined {
    return attrs ? (redactValue(attrs) as Record<string, unknown>) : undefined;
}

export const logger = {
    debug: (message: string, attrs?: Record<string, unknown>) =>
        logfire.debug(message, redactAttrs(attrs)),
    info: (message: string, attrs?: Record<string, unknown>) =>
        logfire.info(message, redactAttrs(attrs)),
    warn: (message: string, attrs?: Record<string, unknown>) =>
        logfire.warning(message, redactAttrs(attrs)),
    error: (message: string, attrs?: Record<string, unknown>) =>
        logfire.error(message, redactAttrs(attrs)),
};

// ── Tracer ──────────────────────────────────────────────────────────────────

export const tracer = trace.getTracer("derp");

/** Run an async function inside a named span with automatic error handling */
export async function withSpan<T>(
    name: string,
    attrs: Record<string, string | number | boolean>,
    fn: (span: Span) => Promise<T>,
): Promise<T> {
    return tracer.startActiveSpan(name, { attributes: attrs }, async (span) => {
        try {
            const result = await fn(span);
            if (!handledFailureSpans.has(span)) {
                span.setStatus({ code: SpanStatusCode.OK });
            }
            return result;
        } catch (err) {
            const redacted = redactedException(err);
            span.setStatus({
                code: SpanStatusCode.ERROR,
                message: redacted.message,
            });
            span.recordException(redacted);
            throw err;
        } finally {
            span.end();
        }
    });
}

// ── Metrics ─────────────────────────────────────────────────────────────────

interface DerpMetrics {
    updatesProcessed: ReturnType<
        ReturnType<typeof otelMetrics.getMeter>["createCounter"]
    >;
    llmRequests: ReturnType<
        ReturnType<typeof otelMetrics.getMeter>["createCounter"]
    >;
    llmTokensInput: ReturnType<
        ReturnType<typeof otelMetrics.getMeter>["createHistogram"]
    >;
    llmTokensOutput: ReturnType<
        ReturnType<typeof otelMetrics.getMeter>["createHistogram"]
    >;
    toolCalls: ReturnType<
        ReturnType<typeof otelMetrics.getMeter>["createCounter"]
    >;
    creditTransactions: ReturnType<
        ReturnType<typeof otelMetrics.getMeter>["createCounter"]
    >;
    creditRevenue: ReturnType<
        ReturnType<typeof otelMetrics.getMeter>["createCounter"]
    >;
    remindersFired: ReturnType<
        ReturnType<typeof otelMetrics.getMeter>["createCounter"]
    >;
    contextTokens: ReturnType<
        ReturnType<typeof otelMetrics.getMeter>["createHistogram"]
    >;
    handledFailures: ReturnType<
        ReturnType<typeof otelMetrics.getMeter>["createCounter"]
    >;
    providerCalls: ReturnType<
        ReturnType<typeof otelMetrics.getMeter>["createCounter"]
    >;
    providerCostMicros: ReturnType<
        ReturnType<typeof otelMetrics.getMeter>["createCounter"]
    >;
}

export let derpMetrics: DerpMetrics;

function initMetrics(): void {
    const meter = otelMetrics.getMeter("derp");

    derpMetrics = {
        updatesProcessed: meter.createCounter("derp.updates", {
            description: "Telegram updates processed by type",
        }),
        llmRequests: meter.createCounter("derp.llm.requests", {
            description: "LLM API calls by model and tier",
        }),
        llmTokensInput: meter.createHistogram("derp.llm.tokens.input", {
            description: "Input token distribution by model",
        }),
        llmTokensOutput: meter.createHistogram("derp.llm.tokens.output", {
            description: "Output token distribution by model",
        }),
        toolCalls: meter.createCounter("derp.tools.calls", {
            description: "Tool invocations by tool and outcome",
        }),
        creditTransactions: meter.createCounter("derp.credits.transactions", {
            description: "Credit movements by type",
        }),
        creditRevenue: meter.createCounter("derp.credits.revenue", {
            description: "Stars received by source",
        }),
        remindersFired: meter.createCounter("derp.reminders.fired", {
            description: "Reminders executed by type",
        }),
        contextTokens: meter.createHistogram("derp.context.tokens", {
            description: "Context window message count by tier",
        }),
        handledFailures: meter.createCounter("derp.failures.handled", {
            description: "Handled user-visible failures by subsystem",
        }),
        providerCalls: meter.createCounter("derp.provider.calls", {
            description:
                "Provider calls by provider, route, operation, and status",
        }),
        providerCostMicros: meter.createCounter("derp.provider.cost_micros", {
            description: "Estimated or actual provider cost in USD micros",
        }),
    };
}

export function recordHandledFailure(
    subsystem: string,
    reason: string,
    attrs: Record<string, string | number | boolean> = {},
): void {
    const span = trace.getActiveSpan();
    const redactedReason = redactString(reason);
    const redactedAttrs = redactFailureAttrs(attrs);
    const metricAttrs = boundedFailureMetricAttrs(subsystem, redactedAttrs);
    if (span) {
        handledFailureSpans.add(span);
        span.setStatus({ code: SpanStatusCode.ERROR, message: redactedReason });
        span.setAttribute("derp.failure.subsystem", subsystem);
        span.setAttribute("derp.failure.reason", redactedReason);
        for (const [key, value] of Object.entries(redactedAttrs)) {
            span.setAttribute(`derp.failure.${key}`, value);
        }
    }
    derpMetrics?.handledFailures.add(1, metricAttrs);
}

function redactFailureAttrs(
    attrs: Record<string, string | number | boolean>,
): Record<string, string | number | boolean> {
    return Object.fromEntries(
        Object.entries(attrs).map(([key, value]) => [
            key,
            typeof value === "string" ? redactString(value) : value,
        ]),
    );
}

export function spanHasHandledFailure(span: Span): boolean {
    return handledFailureSpans.has(span);
}

function boundedFailureMetricAttrs(
    subsystem: string,
    attrs: Record<string, string | number | boolean>,
): Record<string, string | number | boolean> {
    const bounded: Record<string, string | number | boolean> = { subsystem };
    for (const key of ["tool", "model", "outcome", "source", "reason_code"]) {
        const value = attrs[key];
        if (value != null) bounded[key] = value;
    }
    return bounded;
}
