/** Credit gate — wraps tool execution with credit checks and deductions */

import {
	derpMetrics,
	logger,
	recordHandledFailure,
	withSpan,
} from "../common/observability";
import type { CreditCheckResult } from "../credits/types";
import { ModelTier } from "../llm/registry";
import type { ToolContext, ToolDefinition, ToolResult } from "./types";

const TIER_RANK: Record<ModelTier, number> = {
	[ModelTier.FREE]: 0,
	[ModelTier.STANDARD]: 1,
	[ModelTier.PREMIUM]: 2,
};

function zeroCostResult(result: CreditCheckResult): CreditCheckResult {
	return {
		...result,
		creditsToDeduct: 0,
		creditsRemaining: null,
	};
}

/**
 * Execute a tool with credit gating.
 * 1. Check daily free limit
 * 2. Check credits (chat first, then user)
 * 3. Reserve free quota before provider work
 * 4. Execute the tool
 * 5. Deduct paid credits only after successful execution
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

			if (tool.chatAdminOnly && !ctx.isChatAdmin) {
				const rejectReason = "Only chat admins can use this tool";
				span.setAttribute("derp.tool.outcome", "rejected");
				span.setAttribute("derp.tool.reject_reason", rejectReason);
				return {
					text: rejectReason,
					error: rejectReason,
				};
			}

			if (tool.minTier && TIER_RANK[ctx.tier] < TIER_RANK[tool.minTier]) {
				const rejectReason = `This tool requires ${tool.minTier} access`;
				span.setAttribute("derp.tool.outcome", "rejected");
				span.setAttribute("derp.tool.reject_reason", rejectReason);
				return {
					text: `${rejectReason}. Use /buy to upgrade.`,
					error: rejectReason,
				};
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
					text: buildUnavailableMessage(
						tool,
						creditResult.rejectReason,
						upsell,
					),
					error: creditResult.rejectReason,
					creditResult,
				};
			}

			const idempotencyKey = ctx.idempotencyKey;
			if (creditResult.source === "free") {
				try {
					const reserved = await ctx.creditService.deduct(
						creditResult,
						tool.name,
						idempotencyKey,
					);
					if (reserved === "duplicate") {
						span.setAttribute("derp.tool.outcome", "duplicate");
						return {
							text: "This request was already processed.",
							error: "Duplicate request",
							creditResult: zeroCostResult(creditResult),
						};
					}
					if (reserved === "quota_exhausted") {
						const reason = "Daily free limit reached";
						span.setAttribute("derp.tool.outcome", "rejected");
						span.setAttribute("derp.tool.reject_reason", reason);
						return {
							text: buildUnavailableMessage(
								tool,
								reason,
								buildUpsellMessage(tool),
							),
							error: reason,
							creditResult: zeroCostResult(creditResult),
						};
					}
				} catch (err) {
					const error = err instanceof Error ? err.message : String(err);
					span.setAttribute("derp.tool.outcome", "rejected");
					span.setAttribute("derp.tool.reject_reason", error);
					derpMetrics.toolCalls.add(1, {
						tool: tool.name,
						outcome: "rejected",
					});
					recordHandledFailure("tool", error, { tool: tool.name });
					logger.error("tool_free_quota_reservation_failed", {
						tool: tool.name,
						error,
					});
					return {
						text: "I couldn't reserve this free use. Please try again.",
						error,
						creditResult: zeroCostResult(creditResult),
					};
				}
			} else if (idempotencyKey) {
				const existing = await ctx.creditService.hasProcessed(idempotencyKey);
				if (existing) {
					span.setAttribute("derp.tool.outcome", "duplicate");
					return {
						text: "This request was already processed.",
						error: "Duplicate request",
						creditResult: zeroCostResult(creditResult),
					};
				}
			}

			let result: ToolResult;
			try {
				result = await tool.execute(params, ctx);
			} catch (err) {
				const error = err instanceof Error ? err.message : String(err);
				span.setAttribute("derp.tool.outcome", "error");
				derpMetrics.toolCalls.add(1, {
					tool: tool.name,
					outcome: "error",
				});
				recordHandledFailure("tool", error, { tool: tool.name });
				logger.error("tool_execution_failed", { tool: tool.name, error });
				return {
					text: "I couldn't complete that tool request. Please try again later.",
					error,
					creditResult: zeroCostResult(creditResult),
				};
			}

			if (!result.error) {
				if (creditResult.source !== "free") {
					try {
						const charged = await ctx.creditService.deduct(
							creditResult,
							tool.name,
							idempotencyKey,
						);
						if (charged === "duplicate") {
							span.setAttribute("derp.tool.outcome", "duplicate");
							return {
								text: "This request was already processed.",
								error: "Duplicate request",
								creditResult: zeroCostResult(creditResult),
							};
						}
					} catch (err) {
						const error = err instanceof Error ? err.message : String(err);
						span.setAttribute("derp.tool.outcome", "billing_error");
						span.setAttribute("derp.tool.billing_error", error);
						derpMetrics.toolCalls.add(1, {
							tool: tool.name,
							outcome: "billing_error",
						});
						recordHandledFailure("tool_billing", error, { tool: tool.name });
						logger.error("tool_billing_failed_after_success", {
							tool: tool.name,
							error,
						});
						return {
							text: "The result was generated, but billing could not be finalized. No extra action is needed.",
							error,
							creditResult: zeroCostResult(creditResult),
						};
					}
				}

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
				recordHandledFailure("tool", result.error, { tool: tool.name });
				logger.error("tool_returned_error", {
					tool: tool.name,
					error: result.error,
				});
			}

			logger.info("tool_executed", {
				tool: tool.name,
				creditsDeducted: creditResult.creditsToDeduct,
				source: creditResult.source,
				outcome: result.error ? "error" : "success",
			});

			return {
				...(result.error
					? {
							...result,
							handled: false,
							text: safeToolErrorText(tool, result),
						}
					: result),
				creditResult: result.error
					? zeroCostResult(creditResult)
					: creditResult,
			};
		},
	);
}

function buildUpsellMessage(tool: ToolDefinition): string {
	if (tool.credits === 0 && Number.isFinite(tool.freeDaily)) {
		return "Your free daily limit is used up. Try again tomorrow.";
	}
	if (tool.credits >= 100) {
		return "Subscribe from 150⭐/month for the best value. Use /buy to see plans.";
	}
	return "Use /buy to get credits or subscribe.";
}

function buildUnavailableMessage(
	tool: ToolDefinition,
	reason: string | undefined,
	upsell: string,
): string {
	if (tool.credits === 0 && Number.isFinite(tool.freeDaily)) {
		return upsell;
	}
	if (reason?.toLowerCase().includes("credit")) {
		return `${reason}. ${upsell}`;
	}
	return `That tool is not available right now. ${upsell}`;
}

const SAFE_USER_ERRORS = new Set([
	"Missing content",
	"Unauthorized",
	"Unknown action",
	"No source image",
	"Unknown participant",
	"No profile photo",
	"Missing cron",
	"Invalid datetime",
	"Past time",
	"Not found",
]);

function safeToolErrorText(tool: ToolDefinition, result: ToolResult): string {
	if (result.error && SAFE_USER_ERRORS.has(result.error)) {
		return result.text ?? "I need a bit more information to do that.";
	}
	if (tool.category === "utility" && result.text && result.text.length < 240) {
		return result.text;
	}
	return "I couldn't complete that tool request. Please try again later.";
}
