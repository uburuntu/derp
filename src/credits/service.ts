import type { Database } from "../db/connection";
import {
	addChatCredits,
	addUserCredits,
	deductChatCredits,
	deductUserCredits,
	getBalances,
	getDailyUsage,
	getTransactionByIdempotencyKey,
	recordFreeToolUsage,
} from "../db/queries/credits";
import type { Chat, User } from "../db/schema";
import {
	CONTEXT_LIMITS,
	getDefaultModel,
	getModel,
	ModelCapability,
	ModelTier,
} from "../llm/registry";
import type { CreditCheckResult } from "./types";

// ── Tool pricing lookup (kept in sync with ToolRegistry) ─────────────────────

interface ToolPricing {
	credits: number;
	freeDaily: number;
	capability?: ModelCapability;
	defaultModel?: string;
}

const toolPricingMap = new Map<string, ToolPricing>();

/** Register a tool's pricing (called by ToolRegistry on startup) */
export function registerToolPricing(name: string, pricing: ToolPricing): void {
	toolPricingMap.set(name, pricing);
}

export function hasActiveSubscription(
	user: Pick<User, "subscriptionTier" | "subscriptionExpiresAt">,
): boolean {
	return (
		user.subscriptionTier != null &&
		user.subscriptionExpiresAt != null &&
		user.subscriptionExpiresAt > new Date()
	);
}

// ── Credit Service ───────────────────────────────────────────────────────────

export class CreditService {
	constructor(
		private db: Database,
		private user: User,
		private chat: Chat,
	) {}

	/** Get orchestrator config: which model/tier to use for chat */
	async getOrchestratorConfig(): Promise<{
		tier: ModelTier;
		modelId: string;
		contextLimit: number;
	}> {
		const { userCredits, chatCredits } = await getBalances(
			this.db,
			this.user.telegramId,
			this.chat.telegramId,
		);

		const hasPaid =
			chatCredits > 0 || userCredits > 0 || hasActiveSubscription(this.user);
		const tier = hasPaid ? ModelTier.STANDARD : ModelTier.FREE;
		const model = getDefaultModel(ModelCapability.TEXT, tier);
		const contextLimit = CONTEXT_LIMITS[tier];

		return { tier, modelId: model.id, contextLimit };
	}

	/** Check if a tool can be used */
	async checkToolAccess(toolName: string): Promise<CreditCheckResult> {
		const pricing = toolPricingMap.get(toolName);
		if (!pricing) {
			return {
				allowed: false,
				tier: ModelTier.FREE,
				modelId: "",
				source: "rejected",
				creditsToDeduct: 0,
				creditsRemaining: 0,
				freeRemaining: 0,
				rejectReason: `Unknown tool: ${toolName}`,
			};
		}

		// Resolve model
		let model: ReturnType<typeof getModel>;
		if (pricing.defaultModel) {
			model = getModel(pricing.defaultModel);
		} else if (pricing.capability) {
			model = getDefaultModel(pricing.capability, ModelTier.STANDARD);
		} else {
			model = getDefaultModel(ModelCapability.TEXT, ModelTier.STANDARD);
		}

		const totalCost = pricing.credits;
		const hasMeteredFreeQuota =
			Number.isFinite(pricing.freeDaily) && pricing.freeDaily > 0;

		// Check free daily limit first
		if (hasMeteredFreeQuota) {
			const used = await getDailyUsage(
				this.db,
				this.user.id,
				this.chat.id,
				toolName,
			);
			if (used < pricing.freeDaily) {
				return {
					allowed: true,
					tier: model.tier,
					modelId: model.id,
					source: "free",
					creditsToDeduct: 0,
					creditsRemaining: null,
					freeRemaining: pricing.freeDaily - used - 1,
				};
			}
		}

		if (totalCost === 0 && hasMeteredFreeQuota) {
			return {
				allowed: false,
				tier: model.tier,
				modelId: model.id,
				source: "rejected",
				creditsToDeduct: 0,
				creditsRemaining: 0,
				freeRemaining: 0,
				rejectReason: `Daily free limit reached for ${toolName}`,
			};
		}

		// If tool is completely free and unmetered, always allow
		if (totalCost === 0) {
			return {
				allowed: true,
				tier: model.tier,
				modelId: model.id,
				source: "free",
				creditsToDeduct: 0,
				creditsRemaining: null,
				freeRemaining: null,
			};
		}

		// Check balances
		const { userCredits, chatCredits } = await getBalances(
			this.db,
			this.user.telegramId,
			this.chat.telegramId,
		);

		// Chat credits first
		if (chatCredits >= totalCost) {
			return {
				allowed: true,
				tier: model.tier,
				modelId: model.id,
				source: "chat",
				creditsToDeduct: totalCost,
				creditsRemaining: chatCredits - totalCost,
				freeRemaining: null,
			};
		}

		// User credits
		if (userCredits >= totalCost) {
			return {
				allowed: true,
				tier: model.tier,
				modelId: model.id,
				source: "user",
				creditsToDeduct: totalCost,
				creditsRemaining: userCredits - totalCost,
				freeRemaining: null,
			};
		}

		return {
			allowed: false,
			tier: model.tier,
			modelId: model.id,
			source: "rejected",
			creditsToDeduct: 0,
			creditsRemaining: 0,
			freeRemaining: 0,
			rejectReason: `Need ${totalCost} credits for ${toolName}`,
		};
	}

	/** Deduct credits after successful tool execution */
	async deduct(
		result: CreditCheckResult,
		toolName: string,
		idempotencyKey?: string,
		meta?: Record<string, unknown>,
	): Promise<boolean> {
		// Check idempotency
		if (idempotencyKey) {
			const existing = await getTransactionByIdempotencyKey(
				this.db,
				idempotencyKey,
			);
			if (existing) return false;
		}

		if (result.source === "free") {
			const pricing = toolPricingMap.get(toolName);
			if (
				pricing &&
				Number.isFinite(pricing.freeDaily) &&
				pricing.freeDaily > 0
			) {
				return await recordFreeToolUsage(
					this.db,
					this.user.id,
					this.chat.id,
					toolName,
					result.modelId,
					idempotencyKey,
					meta,
				);
			}
		} else if (result.source === "chat") {
			await deductChatCredits(
				this.db,
				this.chat.id,
				this.user.id,
				result.creditsToDeduct,
				toolName,
				result.modelId,
				idempotencyKey,
				meta,
			);
			return true;
		} else if (result.source === "user") {
			await deductUserCredits(
				this.db,
				this.user.id,
				result.creditsToDeduct,
				toolName,
				result.modelId,
				idempotencyKey,
				meta,
			);
			return true;
		}
		return true;
	}

	/** Refund a paid pre-execution reservation after a tool reports failure. */
	async refundDeduction(
		result: CreditCheckResult,
		toolName: string,
		idempotencyKey?: string,
		meta?: Record<string, unknown>,
	): Promise<void> {
		if (result.creditsToDeduct <= 0) return;
		const refundKey = idempotencyKey ? `${idempotencyKey}:refund` : undefined;
		const refundMeta = {
			...meta,
			reason: "tool_failed_after_reservation",
			toolName,
			modelId: result.modelId,
		};

		if (result.source === "chat") {
			await addChatCredits(
				this.db,
				this.chat.id,
				this.user.id,
				result.creditsToDeduct,
				"refund",
				undefined,
				refundKey,
				refundMeta,
			);
			return;
		}

		if (result.source === "user") {
			await addUserCredits(
				this.db,
				this.user.id,
				result.creditsToDeduct,
				"refund",
				undefined,
				refundKey,
				refundMeta,
			);
		}
	}

	/** Grant welcome bonus (25 credits, idempotent) */
	async grantWelcomeBonus(): Promise<boolean> {
		const key = `welcome:${this.user.telegramId}`;
		const existing = await getTransactionByIdempotencyKey(this.db, key);
		if (existing) return false;

		await addUserCredits(this.db, this.user.id, 25, "grant", undefined, key, {
			reason: "welcome_bonus",
		});
		return true;
	}
}
