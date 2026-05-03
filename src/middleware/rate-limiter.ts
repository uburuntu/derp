/** Rate limiter middleware — per-user incoming rate limiting */

import { limit } from "@grammyjs/ratelimiter";
import type { DerpContext } from "../bot/context";
import { logger } from "../common/observability";

/** Create rate limiter: 3 messages per 2 seconds per user */
export function createRateLimiter() {
	return limit({
		timeFrame: 2000,
		limit: 3,
		keyGenerator: (ctx: DerpContext) => {
			if (
				ctx.preCheckoutQuery ||
				ctx.message?.successful_payment ||
				ctx.message?.refunded_payment
			) {
				return undefined;
			}
			return ctx.from?.id.toString();
		},
		onLimitExceeded: async (ctx: DerpContext) => {
			logger.warn("rate_limit_exceeded", {
				userId: ctx.from?.id,
				chatId: ctx.chat?.id,
			});
		},
	});
}
