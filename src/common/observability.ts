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

export function initObservability(cfg: Config): void {
	if (initialized) return;

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

export const logger = {
	debug: (message: string, attrs?: Record<string, unknown>) =>
		logfire.debug(message, attrs),
	info: (message: string, attrs?: Record<string, unknown>) =>
		logfire.info(message, attrs),
	warn: (message: string, attrs?: Record<string, unknown>) =>
		logfire.warning(message, attrs),
	error: (message: string, attrs?: Record<string, unknown>) =>
		logfire.error(message, attrs),
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
			span.setStatus({ code: SpanStatusCode.OK });
			return result;
		} catch (err) {
			span.setStatus({
				code: SpanStatusCode.ERROR,
				message: err instanceof Error ? err.message : String(err),
			});
			span.recordException(err instanceof Error ? err : new Error(String(err)));
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
}

// biome-ignore lint: initialized in initMetrics before any use
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
	};
}
