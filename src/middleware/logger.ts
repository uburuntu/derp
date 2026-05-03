/** Logger middleware — root span per update + structured logging */

import { SpanStatusCode } from "@opentelemetry/api";
import type { NextFunction } from "grammy";
import type { DerpContext } from "../bot/context";
import { derpMetrics, logger, tracer } from "../common/observability";

export async function loggerMiddleware(
	ctx: DerpContext,
	next: NextFunction,
): Promise<void> {
	const updateType =
		Object.keys(ctx.update).filter((k) => k !== "update_id")[0] ?? "unknown";
	const updateId = ctx.update.update_id;
	const chatId = ctx.chat?.id ?? 0;
	const userId = ctx.from?.id ?? 0;

	derpMetrics.updatesProcessed.add(1, { type: updateType });

	await tracer.startActiveSpan(
		`update.${updateType}`,
		{
			attributes: {
				"derp.update.id": updateId,
				"derp.update.type": updateType,
				"derp.chat.id": chatId,
				"derp.user.id": userId,
			},
		},
		async (span) => {
			const start = performance.now();

			logger.info("update_received", {
				updateId,
				type: updateType,
				chatId,
				userId,
			});

			try {
				await next();
				span.setStatus({ code: SpanStatusCode.OK });
			} catch (err) {
				span.setStatus({
					code: SpanStatusCode.ERROR,
					message: err instanceof Error ? err.message : String(err),
				});
				span.recordException(
					err instanceof Error ? err : new Error(String(err)),
				);
				throw err;
			} finally {
				const durationMs = Math.round(performance.now() - start);
				span.setAttribute("derp.duration_ms", durationMs);
				span.end();

				logger.info("update_processed", {
					updateId,
					type: updateType,
					durationMs,
				});
			}
		},
	);
}
