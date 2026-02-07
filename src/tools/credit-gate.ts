/** Credit gate — wraps tool execution with credit checks and deductions */

import { derpMetrics, logger, withSpan } from "../common/observability";
import type { CreditCheckResult } from "../credits/types";
import type { ToolContext, ToolDefinition, ToolResult } from "./types";

/**
 * Execute a tool with credit gating.
 * 1. Check daily free limit
 * 2. Check credits (chat first, then user)
 * 3. Execute the tool
 * 4. Deduct credits on success
 */
export async function executeWithCreditGate(
	tool: ToolDefinition,
	params: unknown,
	ctx: ToolContext,
): Promise<ToolResult & { creditResult?: CreditCheckResult }> {
	return withSpan(
		`tool.${tool.name}`,
		{
			"derp.tool.name": tool.name,
			"derp.tool.category": tool.category,
			"derp.tool.credits": tool.credits,
		},
		async (span) => {
			// Free tools (0 credits, unlimited daily) skip gating
			if (tool.credits === 0 && tool.freeDaily === Number.POSITIVE_INFINITY) {
				const result = await tool.execute(params, ctx);
				span.setAttribute("derp.tool.outcome", "free");
				derpMetrics.toolCalls.add(1, {
					tool: tool.name,
					outcome: "success",
				});
				return { ...result };
			}

			// Check access
			const creditResult = await ctx.creditService.checkToolAccess(tool.name);

			if (!creditResult.allowed) {
				span.setAttribute("derp.tool.outcome", "rejected");
				span.setAttribute(
					"derp.tool.reject_reason",
					creditResult.rejectReason ?? "",
				);
				derpMetrics.toolCalls.add(1, {
					tool: tool.name,
					outcome: "rejected",
				});
				logger.info("tool_access_denied", {
					tool: tool.name,
					reason: creditResult.rejectReason,
				});
				const upsell = buildUpsellMessage(tool);
				return {
					text: `[TOOL_UNAVAILABLE] ${creditResult.rejectReason}. ${upsell}`,
					error: creditResult.rejectReason,
					creditResult,
				};
			}

			// Execute the tool
			const result = await tool.execute(params, ctx);

			// Deduct credits only on success (no error)
			if (!result.error) {
				const idempotencyKey = `tool:${tool.name}:${Date.now()}`;
				await ctx.creditService.deduct(creditResult, tool.name, idempotencyKey);
				span.setAttribute("derp.tool.outcome", "success");
				span.setAttribute(
					"derp.credits.deducted",
					creditResult.creditsToDeduct,
				);
				span.setAttribute("derp.credits.source", creditResult.source);
				derpMetrics.toolCalls.add(1, {
					tool: tool.name,
					outcome: "success",
				});
				if (creditResult.creditsToDeduct > 0) {
					derpMetrics.creditTransactions.add(1, { type: "spend" });
				}
			} else {
				span.setAttribute("derp.tool.outcome", "error");
				derpMetrics.toolCalls.add(1, {
					tool: tool.name,
					outcome: "error",
				});
			}

			logger.info("tool_executed", {
				tool: tool.name,
				creditsDeducted: creditResult.creditsToDeduct,
				source: creditResult.source,
				outcome: result.error ? "error" : "success",
			});

			return { ...result, creditResult };
		},
	);
}

function buildUpsellMessage(tool: ToolDefinition): string {
	if (tool.credits >= 100) {
		return "Subscribe from 150⭐/month for the best value. Use /buy to see plans.";
	}
	return "Use /buy to get credits or subscribe.";
}
