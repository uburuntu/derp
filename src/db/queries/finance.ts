import { and, asc, eq, sql } from "drizzle-orm";
import type { Database } from "../connection";
import {
	creditDebtEvents,
	creditDebts,
	ledger,
	pendingToolConfirmations,
	providerCalls,
	quotaWindows,
	users,
} from "../schema";

type FinanceTransaction = Parameters<Parameters<Database["transaction"]>[0]>[0];

class GlobalQuotaExhaustedError extends Error {
	constructor() {
		super("Global quota exhausted");
	}
}

export type CreditDebtTarget = "user" | "chat";
export type QuotaScope = "free_chat" | "web_search" | "inline" | "promo_media";

export interface StartProviderCallInput {
	logicalRequestKey?: string;
	attemptNo?: number;
	provider: string;
	operation: string;
	modelId: string;
	route?: "primary" | "fallback";
	keyClass?: "free" | "paid";
	userId?: string | null;
	chatId?: string | null;
	ledgerId?: string | null;
	messageId?: string | null;
	toolName?: string | null;
	creditsCharged?: number;
	creditSource?: string | null;
	estimatedCostMicros?: number;
	mediaInputCount?: number;
	meta?: Record<string, unknown>;
}

export interface FinishProviderCallInput {
	actualModelId?: string | null;
	inputTokens?: number;
	outputTokens?: number;
	cacheHitTokens?: number;
	mediaInputCount?: number;
	mediaOutputCount?: number;
	durationSeconds?: number | null;
	estimatedCostMicros?: number;
	actualCostMicros?: number;
	providerRequestId?: string | null;
	finishReason?: string | null;
	meta?: Record<string, unknown>;
}

export interface FailProviderCallInput {
	errorCode?: string | null;
	errorMessage?: string | null;
	estimatedCostMicros?: number;
	actualCostMicros?: number;
	meta?: Record<string, unknown>;
}

export interface PendingConfirmationInput {
	userId: string;
	chatId: string;
	telegramChatId: number;
	messageId?: number | null;
	threadId?: number | null;
	toolName: string;
	params: Record<string, unknown>;
	cost: number;
	source: "user" | "chat";
	idempotencyKey: string;
	expiresAt: Date;
	meta?: Record<string, unknown>;
}

export function usdToMicros(value: number): number {
	if (!Number.isFinite(value) || value <= 0) return 0;
	return Math.max(0, Math.round(value * 1_000_000));
}

export function dailyWindowKey(date = new Date()): string {
	return date.toISOString().slice(0, 10);
}

export function quotaSubjectKey(input: {
	userId?: string | null;
	chatId?: string | null;
	botWide?: boolean;
}): string {
	if (input.botWide) return "bot";
	if (input.chatId) return `chat:${input.chatId}`;
	if (input.userId) return `user:${input.userId}`;
	throw new Error("Quota subject requires userId, chatId, or botWide");
}

export async function startProviderCall(
	db: Database,
	input: StartProviderCallInput,
): Promise<string> {
	const [inserted] = await db
		.insert(providerCalls)
		.values({
			logicalRequestKey: input.logicalRequestKey,
			attemptNo: input.attemptNo ?? 1,
			provider: input.provider,
			operation: input.operation,
			route: input.route ?? "primary",
			keyClass: input.keyClass ?? "free",
			modelId: input.modelId,
			userId: input.userId ?? null,
			chatId: input.chatId ?? null,
			ledgerId: input.ledgerId ?? null,
			messageId: input.messageId ?? null,
			toolName: input.toolName ?? null,
			creditsCharged: input.creditsCharged ?? 0,
			creditSource: input.creditSource ?? null,
			estimatedCostMicros: input.estimatedCostMicros ?? 0,
			mediaInputCount: input.mediaInputCount ?? 0,
			meta: input.meta,
		})
		.onConflictDoUpdate({
			target: [providerCalls.logicalRequestKey, providerCalls.attemptNo],
			set: {
				status: "started",
				updatedAt: new Date(),
			},
		})
		.returning({ id: providerCalls.id });

	if (!inserted) throw new Error("Provider call insert failed");
	return inserted.id;
}

export async function finishProviderCall(
	db: Database,
	id: string,
	input: FinishProviderCallInput,
): Promise<void> {
	await db
		.update(providerCalls)
		.set({
			status: "succeeded",
			actualModelId: input.actualModelId ?? undefined,
			inputTokens: input.inputTokens ?? 0,
			outputTokens: input.outputTokens ?? 0,
			cacheHitTokens: input.cacheHitTokens ?? 0,
			mediaInputCount: input.mediaInputCount ?? undefined,
			mediaOutputCount: input.mediaOutputCount ?? undefined,
			durationSeconds: input.durationSeconds ?? undefined,
			estimatedCostMicros: input.estimatedCostMicros ?? undefined,
			actualCostMicros: input.actualCostMicros ?? undefined,
			providerRequestId: input.providerRequestId ?? undefined,
			finishReason: input.finishReason ?? undefined,
			meta: input.meta ?? undefined,
			finishedAt: new Date(),
			updatedAt: new Date(),
		})
		.where(eq(providerCalls.id, id));
}

export async function failProviderCall(
	db: Database,
	id: string,
	input: FailProviderCallInput,
): Promise<void> {
	await db
		.update(providerCalls)
		.set({
			status: "failed",
			errorCode: input.errorCode ?? "provider_error",
			errorMessage: input.errorMessage?.slice(0, 500),
			estimatedCostMicros: input.estimatedCostMicros ?? undefined,
			actualCostMicros: input.actualCostMicros ?? undefined,
			meta: input.meta ?? undefined,
			finishedAt: new Date(),
			updatedAt: new Date(),
		})
		.where(eq(providerCalls.id, id));
}

export async function reserveQuotaWindow(
	db: Database | FinanceTransaction,
	input: {
		scope: QuotaScope;
		limit: number;
		amount?: number;
		userId?: string | null;
		chatId?: string | null;
		botWide?: boolean;
		windowKey?: string;
		meta?: Record<string, unknown>;
	},
): Promise<boolean> {
	const amount = input.amount ?? 1;
	const windowKey = input.windowKey ?? dailyWindowKey();
	const subjectKey = quotaSubjectKey(input);

	const rows = await db.execute(sql`
		INSERT INTO ${quotaWindows} (
			${quotaWindows.scope},
			${quotaWindows.subjectKey},
			${quotaWindows.userId},
			${quotaWindows.chatId},
			${quotaWindows.windowKey},
			${quotaWindows.used},
			${quotaWindows.limit},
			${quotaWindows.meta}
		)
		VALUES (
			${input.scope},
			${subjectKey},
			${input.userId ?? null},
			${input.chatId ?? null},
			${windowKey},
			${amount},
			${input.limit},
			${input.meta ? JSON.stringify(input.meta) : null}::jsonb
		)
		ON CONFLICT (
			${quotaWindows.scope},
			${quotaWindows.subjectKey},
			${quotaWindows.windowKey}
		)
		DO UPDATE SET
			${quotaWindows.used} = ${quotaWindows.used} + ${amount},
			${quotaWindows.limit} = GREATEST(${quotaWindows.limit}, ${input.limit}),
			${quotaWindows.updatedAt} = now()
		WHERE ${quotaWindows.used} + ${amount} <= ${quotaWindows.limit}
		RETURNING ${quotaWindows.id}
	`);

	return rows.length > 0;
}

export async function getQuotaUsage(
	db: Database,
	input: {
		scope: QuotaScope;
		userId?: string | null;
		chatId?: string | null;
		botWide?: boolean;
		windowKey?: string;
	},
): Promise<number> {
	const [row] = await db
		.select({ used: quotaWindows.used })
		.from(quotaWindows)
		.where(
			and(
				eq(quotaWindows.scope, input.scope),
				eq(quotaWindows.subjectKey, quotaSubjectKey(input)),
				eq(quotaWindows.windowKey, input.windowKey ?? dailyWindowKey()),
			),
		)
		.limit(1);
	return row?.used ?? 0;
}

export async function recordGlobalFreeToolUsage(
	db: Database,
	input: {
		userId: string;
		chatId: string;
		toolName: string;
		modelId: string | null;
		limit: number;
		idempotencyKey?: string;
		meta?: Record<string, unknown>;
	},
): Promise<"applied" | "duplicate" | "quota_exhausted"> {
	try {
		return await db.transaction(async (tx) => {
			if (input.idempotencyKey) {
				const [inserted] = await tx
					.insert(ledger)
					.values({
						userId: input.userId,
						chatId: input.chatId,
						type: "spend",
						amount: 0,
						balanceAfter: 0,
						toolName: input.toolName,
						modelId: input.modelId,
						idempotencyKey: input.idempotencyKey,
						meta: input.meta,
					})
					.onConflictDoNothing({ target: ledger.idempotencyKey })
					.returning({ id: ledger.id });
				if (!inserted) return "duplicate";

				const [userRow] = await tx
					.select({ credits: users.credits })
					.from(users)
					.where(eq(users.id, input.userId))
					.limit(1);

				await tx
					.update(ledger)
					.set({ balanceAfter: userRow?.credits ?? 0 })
					.where(eq(ledger.id, inserted.id));
			}

			const reserved = await reserveQuotaWindow(tx, {
				scope: input.toolName === "webSearch" ? "web_search" : "promo_media",
				userId: input.userId,
				limit: input.limit,
				meta: { ...input.meta, toolName: input.toolName },
			});
			if (!reserved) throw new GlobalQuotaExhaustedError();
			return "applied";
		});
	} catch (err) {
		if (err instanceof GlobalQuotaExhaustedError) return "quota_exhausted";
		throw err;
	}
}

export async function getOpenDebtAmount(
	db: Database | FinanceTransaction,
	input: { userId: string; chatId?: string | null },
): Promise<number> {
	const rows = await db
		.select({ amount: creditDebts.outstandingAmount })
		.from(creditDebts)
		.where(
			input.chatId
				? and(
						eq(creditDebts.chatId, input.chatId),
						eq(creditDebts.status, "open"),
					)
				: and(
						eq(creditDebts.userId, input.userId),
						sql`${creditDebts.chatId} IS NULL`,
						eq(creditDebts.status, "open"),
					),
		);

	return rows.reduce((sum, row) => sum + row.amount, 0);
}

export async function createCreditDebt(
	db: FinanceTransaction,
	input: {
		userId: string;
		chatId?: string | null;
		paymentReceiptId?: string | null;
		telegramChargeId?: string | null;
		target: CreditDebtTarget;
		amount: number;
		meta?: Record<string, unknown>;
	},
): Promise<string> {
	const [debt] = await db
		.insert(creditDebts)
		.values({
			userId: input.userId,
			chatId: input.chatId ?? null,
			paymentReceiptId: input.paymentReceiptId ?? null,
			telegramChargeId: input.telegramChargeId ?? null,
			target: input.target,
			amount: input.amount,
			outstandingAmount: input.amount,
			meta: input.meta,
		})
		.returning({ id: creditDebts.id });

	if (!debt) throw new Error("Credit debt insert failed");

	await db.insert(creditDebtEvents).values({
		debtId: debt.id,
		userId: input.userId,
		chatId: input.chatId ?? null,
		paymentReceiptId: input.paymentReceiptId ?? null,
		type: "created",
		amount: input.amount,
		meta: input.meta,
	});

	return debt.id;
}

export async function settleOpenDebts(
	db: FinanceTransaction,
	input: {
		userId: string;
		chatId?: string | null;
		amount: number;
		paymentReceiptId?: string | null;
		ledgerId?: string | null;
		meta?: Record<string, unknown>;
	},
): Promise<{ settledAmount: number; remainingAmount: number }> {
	let remaining = input.amount;
	let settledAmount = 0;

	const debts = await db
		.select()
		.from(creditDebts)
		.where(
			input.chatId
				? and(
						eq(creditDebts.chatId, input.chatId),
						eq(creditDebts.status, "open"),
					)
				: and(
						eq(creditDebts.userId, input.userId),
						sql`${creditDebts.chatId} IS NULL`,
						eq(creditDebts.status, "open"),
					),
		)
		.orderBy(asc(creditDebts.createdAt))
		.for("update");

	for (const debt of debts) {
		if (remaining <= 0) break;
		const recovered = Math.min(remaining, debt.outstandingAmount);
		if (recovered <= 0) continue;
		const outstanding = debt.outstandingAmount - recovered;
		const isSettled = outstanding === 0;

		await db
			.update(creditDebts)
			.set({
				recoveredAmount: debt.recoveredAmount + recovered,
				outstandingAmount: outstanding,
				status: isSettled ? "settled" : "open",
				settledAt: isSettled ? new Date() : undefined,
				updatedAt: new Date(),
			})
			.where(eq(creditDebts.id, debt.id));

		await db.insert(creditDebtEvents).values({
			debtId: debt.id,
			userId: debt.userId,
			chatId: debt.chatId,
			ledgerId: input.ledgerId ?? null,
			paymentReceiptId: input.paymentReceiptId ?? null,
			type: "recovered",
			amount: recovered,
			meta: { ...input.meta, settledByUserId: input.userId },
		});

		remaining -= recovered;
		settledAmount += recovered;
	}

	return { settledAmount, remainingAmount: remaining };
}

export async function waiveCreditDebt(
	db: Database,
	input: { debtId: string; adminId: number; meta?: Record<string, unknown> },
): Promise<boolean> {
	return db.transaction(async (tx) => {
		const [debt] = await tx
			.select()
			.from(creditDebts)
			.where(eq(creditDebts.id, input.debtId))
			.for("update")
			.limit(1);
		if (!debt || debt.status !== "open") return false;

		await tx
			.update(creditDebts)
			.set({
				status: "waived",
				outstandingAmount: 0,
				settledAt: new Date(),
				updatedAt: new Date(),
				meta: { ...debt.meta, ...input.meta, waivedBy: input.adminId },
			})
			.where(eq(creditDebts.id, debt.id));

		await tx.insert(creditDebtEvents).values({
			debtId: debt.id,
			userId: debt.userId,
			chatId: debt.chatId,
			type: "waived",
			amount: debt.outstandingAmount,
			meta: { ...input.meta, waivedBy: input.adminId },
		});

		return true;
	});
}

export async function createPendingToolConfirmation(
	db: Database,
	input: PendingConfirmationInput,
): Promise<string> {
	const [row] = await db
		.insert(pendingToolConfirmations)
		.values({
			userId: input.userId,
			chatId: input.chatId,
			telegramChatId: input.telegramChatId,
			messageId: input.messageId ?? null,
			threadId: input.threadId ?? null,
			toolName: input.toolName,
			params: input.params,
			cost: input.cost,
			source: input.source,
			idempotencyKey: input.idempotencyKey,
			expiresAt: input.expiresAt,
			meta: input.meta,
		})
		.onConflictDoUpdate({
			target: pendingToolConfirmations.idempotencyKey,
			set: {
				status: "pending",
				params: input.params,
				cost: input.cost,
				source: input.source,
				expiresAt: input.expiresAt,
				updatedAt: new Date(),
			},
		})
		.returning({ id: pendingToolConfirmations.id });

	if (!row) throw new Error("Pending confirmation insert failed");
	return row.id;
}

export async function claimPendingToolConfirmation(
	db: Database,
	input: { id: string; userId: string; source?: "user" | "chat" },
): Promise<typeof pendingToolConfirmations.$inferSelect | null> {
	return db.transaction(async (tx) => {
		const [row] = await tx
			.select()
			.from(pendingToolConfirmations)
			.where(
				and(
					eq(pendingToolConfirmations.id, input.id),
					eq(pendingToolConfirmations.userId, input.userId),
					eq(pendingToolConfirmations.status, "pending"),
					sql`${pendingToolConfirmations.expiresAt} > now()`,
				),
			)
			.for("update")
			.limit(1);
		if (!row) return null;

		await tx
			.update(pendingToolConfirmations)
			.set({
				status: "confirmed",
				source: input.source ?? row.source,
				updatedAt: new Date(),
			})
			.where(eq(pendingToolConfirmations.id, row.id));

		return { ...row, source: input.source ?? row.source };
	});
}

export async function getPendingToolConfirmation(
	db: Database,
	id: string,
): Promise<typeof pendingToolConfirmations.$inferSelect | null> {
	const [row] = await db
		.select()
		.from(pendingToolConfirmations)
		.where(eq(pendingToolConfirmations.id, id))
		.limit(1);
	return row ?? null;
}

export async function cancelPendingToolConfirmation(
	db: Database,
	input: { id: string; userId: string },
): Promise<boolean> {
	const rows = await db
		.update(pendingToolConfirmations)
		.set({ status: "cancelled", updatedAt: new Date() })
		.where(
			and(
				eq(pendingToolConfirmations.id, input.id),
				eq(pendingToolConfirmations.userId, input.userId),
				eq(pendingToolConfirmations.status, "pending"),
			),
		)
		.returning({ id: pendingToolConfirmations.id });
	return rows.length > 0;
}
