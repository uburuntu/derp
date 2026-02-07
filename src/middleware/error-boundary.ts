/** Error boundary middleware — catches all errors and sends a user-facing message */

import type { NextFunction } from "grammy";
import type { DerpContext } from "../bot/context";
import { logger } from "../common/observability";

export async function errorBoundary(
	ctx: DerpContext,
	next: NextFunction,
): Promise<void> {
	try {
		await next();
	} catch (err) {
		const errorMsg = err instanceof Error ? err.message : String(err);
		logger.error("update_error", {
			updateId: ctx.update.update_id,
			chatId: ctx.chat?.id,
			userId: ctx.from?.id,
			error: errorMsg,
		});

		try {
			if (ctx.chat) {
				await ctx.reply("Something went wrong. Please try again.", {
					reply_to_message_id: ctx.msg?.message_id,
				});
			}
		} catch {
			logger.error("error_reply_failed", {
				updateId: ctx.update.update_id,
			});
		}
	}
}
