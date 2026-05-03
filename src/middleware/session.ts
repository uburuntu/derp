/** Session middleware — loads credit balances, determines tier, injects CreditService */

import type { NextFunction } from "grammy";
import type { DerpContext } from "../bot/context";
import { CreditService } from "../credits/service";
import { ModelTier } from "../llm/registry";

export async function sessionMiddleware(
	ctx: DerpContext,
	next: NextFunction,
): Promise<void> {
	// Only inject credit service if we have both user and chat
	if (ctx.dbUser && ctx.dbChat) {
		ctx.creditService = new CreditService(ctx.db, ctx.dbUser, ctx.dbChat);
		const config = await ctx.creditService.getOrchestratorConfig();
		ctx.tier = config.tier;
	} else {
		ctx.tier = ModelTier.FREE;
	}

	await next();
}
