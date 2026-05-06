import { and, desc, eq, sql } from "drizzle-orm";
import type { Database } from "../connection";
import {
	chats,
	ledger,
	paymentReceipts,
	subscriptionPeriods,
	usageQuotas,
	users,
} from "../schema";
import { createCreditDebt, settleOpenDebts } from "./finance";

type CreditTransaction = Parameters<Parameters<Database["transaction"]>[0]>[0];
export type ToolDebitResult = "applied" | "duplicate" | "quota_exhausted";

export interface IdempotentCreditResult {
	balanceAfter: number;
	applied: boolean;
}

export interface RefundReconciliationResult {
	applied: boolean;
	target: "user" | "chat";
	originalType: string;
	originalAmount: number;
	recoveredAmount: number;
	unrecoveredAmount: number;
	balanceAfter: number;
}

export interface StarsPaymentRecord {
	userId: string;
	chatId?: string | null;
	telegramChargeId: string;
	providerChargeId?: string | null;
	invoicePayload?: string | null;
	currency: string;
	stars: number;
	productType: "subscription" | "pack" | "donation";
	productId?: string | null;
	creditTarget: "user" | "chat" | "none";
	credits: number;
	meta?: Record<string, unknown>;
}

/** Get both user and chat credit balances */
export async function getBalances(
	db: Database,
	userTelegramId: number,
	chatTelegramId: number,
): Promise<{ userCredits: number; chatCredits: number }> {
	const [userRow] = await db
		.select({ credits: users.credits })
		.from(users)
		.where(eq(users.telegramId, userTelegramId))
		.limit(1);

	const [chatRow] = await db
		.select({ credits: chats.credits })
		.from(chats)
		.where(eq(chats.telegramId, chatTelegramId))
		.limit(1);

	return {
		userCredits: userRow?.credits ?? 0,
		chatCredits: chatRow?.credits ?? 0,
	};
}

async function recordPaymentReceiptIn(
	tx: CreditTransaction,
	record: StarsPaymentRecord,
): Promise<{ id: string; needsSettlement: boolean }> {
	const [inserted] = await tx
		.insert(paymentReceipts)
		.values({
			userId: record.userId,
			chatId: record.chatId ?? null,
			telegramChargeId: record.telegramChargeId,
			providerChargeId: record.providerChargeId ?? null,
			invoicePayload: record.invoicePayload ?? null,
			currency: record.currency,
			stars: record.stars,
			productType: record.productType,
			productId: record.productId ?? null,
			creditTarget: record.creditTarget,
			credits: record.credits,
			status: "received",
			meta: record.meta,
		})
		.onConflictDoNothing({ target: paymentReceipts.telegramChargeId })
		.returning({ id: paymentReceipts.id });

	if (inserted) return { id: inserted.id, needsSettlement: true };

	const [existing] = await tx
		.select({ id: paymentReceipts.id, status: paymentReceipts.status })
		.from(paymentReceipts)
		.where(eq(paymentReceipts.telegramChargeId, record.telegramChargeId))
		.limit(1);

	if (!existing) throw new Error("Payment receipt conflict without row");
	return {
		id: existing.id,
		needsSettlement:
			existing.status === "received" ||
			existing.status === "settlement_failed",
	};
}

async function markPaymentSettledIn(
	tx: CreditTransaction,
	receiptId: string,
): Promise<void> {
	await tx
		.update(paymentReceipts)
		.set({ status: "settled", settledAt: new Date() })
		.where(eq(paymentReceipts.id, receiptId));
}

export async function recordDonationPayment(
	db: Database,
	record: StarsPaymentRecord,
): Promise<IdempotentCreditResult> {
	return db.transaction(async (tx) => {
		const receipt = await recordPaymentReceiptIn(tx, record);
		const refundKey = `donation:${record.telegramChargeId}`;
		if (!receipt.needsSettlement) {
			const [existing] = await tx
				.select({ balanceAfter: ledger.balanceAfter })
				.from(ledger)
				.where(eq(ledger.idempotencyKey, refundKey))
				.limit(1);
			return { balanceAfter: existing?.balanceAfter ?? 0, applied: false };
		}

		const [userRow] = await tx
			.select({ credits: users.credits })
			.from(users)
			.where(eq(users.id, record.userId))
			.limit(1);

		await tx.insert(ledger).values({
			userId: record.userId,
			chatId: record.chatId ?? null,
			type: "donation",
			amount: 0,
			balanceAfter: userRow?.credits ?? 0,
			telegramChargeId: record.telegramChargeId,
			idempotencyKey: refundKey,
			meta: { paymentReceiptId: receipt.id },
		});
		await markPaymentSettledIn(tx, receipt.id);

		return { balanceAfter: userRow?.credits ?? 0, applied: true };
	});
}

/** Get daily usage count for a specific tool */
export async function getDailyUsage(
	db: Database,
	userId: string,
	chatId: string,
	toolName: string,
): Promise<number> {
	const today = new Date().toISOString().slice(0, 10); // YYYY-MM-DD
	const [row] = await db
		.select({ usage: usageQuotas.usage })
		.from(usageQuotas)
		.where(
			and(
				eq(usageQuotas.userId, userId),
				eq(usageQuotas.chatId, chatId),
				eq(usageQuotas.usageDate, today as string),
			),
		)
		.limit(1);

	if (!row?.usage) return 0;
	return (row.usage as Record<string, number>)[toolName] ?? 0;
}

async function incrementDailyUsageIn(
	db: Pick<Database, "insert">,
	userId: string,
	chatId: string,
	toolName: string,
): Promise<void> {
	const today = new Date().toISOString().slice(0, 10);

	await db
		.insert(usageQuotas)
		.values({
			userId,
			chatId,
			usageDate: today as string,
			usage: { [toolName]: 1 },
		})
		.onConflictDoUpdate({
			target: [usageQuotas.userId, usageQuotas.chatId, usageQuotas.usageDate],
			set: {
				usage: sql`jsonb_set(
					COALESCE(${usageQuotas.usage}, '{}'),
					${`{${toolName}}`},
					(COALESCE((${usageQuotas.usage}->${toolName})::int, 0) + 1)::text::jsonb
				)`,
			},
		});
}

class QuotaExhaustedError extends Error {
	constructor() {
		super("Daily free quota exhausted");
	}
}

async function reserveDailyUsageIn(
	db: Pick<Database, "execute">,
	userId: string,
	chatId: string,
	toolName: string,
	limit: number,
): Promise<void> {
	const today = new Date().toISOString().slice(0, 10);
	const rows = await db.execute(sql`
		INSERT INTO ${usageQuotas} (
			${usageQuotas.userId},
			${usageQuotas.chatId},
			${usageQuotas.usageDate},
			${usageQuotas.usage}
		)
		VALUES (
			${userId},
			${chatId},
			${today},
			jsonb_build_object(${toolName}, 1)
		)
		ON CONFLICT (
			${usageQuotas.userId},
			${usageQuotas.chatId},
			${usageQuotas.usageDate}
		)
		DO UPDATE SET
			${usageQuotas.usage} = jsonb_set(
				COALESCE(${usageQuotas.usage}, '{}'::jsonb),
				ARRAY[${toolName}]::text[],
				to_jsonb(COALESCE((${usageQuotas.usage}->>${toolName})::int, 0) + 1),
				true
			),
			${usageQuotas.updatedAt} = now()
		WHERE COALESCE((${usageQuotas.usage}->>${toolName})::int, 0) < ${limit}
		RETURNING ${usageQuotas.usage}
	`);

	if (rows.length === 0) {
		throw new QuotaExhaustedError();
	}
}

/** Increment daily usage for a tool */
export async function incrementDailyUsage(
	db: Database,
	userId: string,
	chatId: string,
	toolName: string,
): Promise<void> {
	await incrementDailyUsageIn(db, userId, chatId, toolName);
}

/** Deduct credits from user balance and record in ledger (atomic) */
export async function deductUserCredits(
	db: Database,
	userId: string,
	amount: number,
	toolName: string,
	modelId: string | null,
	idempotencyKey?: string,
	meta?: Record<string, unknown>,
): Promise<IdempotentCreditResult> {
	return db.transaction(async (tx) => {
		if (idempotencyKey) {
			const [inserted] = await tx
				.insert(ledger)
				.values({
					userId,
					type: "spend",
					amount: -amount,
					balanceAfter: 0,
					toolName,
					modelId,
					idempotencyKey,
					meta,
				})
				.onConflictDoNothing({ target: ledger.idempotencyKey })
				.returning({ id: ledger.id });

			if (!inserted) {
				const [existing] = await tx
					.select({ balanceAfter: ledger.balanceAfter })
					.from(ledger)
					.where(eq(ledger.idempotencyKey, idempotencyKey))
					.limit(1);
				return { balanceAfter: existing?.balanceAfter ?? 0, applied: false };
			}

			const [updated] = await tx
				.update(users)
				.set({
					credits: sql`${users.credits} - ${amount}`,
				})
				.where(and(eq(users.id, userId), sql`${users.credits} >= ${amount}`))
				.returning({ credits: users.credits });

			if (!updated) throw new Error("Insufficient credits");

			await tx
				.update(ledger)
				.set({ balanceAfter: updated.credits })
				.where(eq(ledger.id, inserted.id));

			return { balanceAfter: updated.credits, applied: true };
		}

		const [updated] = await tx
			.update(users)
			.set({
				credits: sql`${users.credits} - ${amount}`,
			})
			.where(and(eq(users.id, userId), sql`${users.credits} >= ${amount}`))
			.returning({ credits: users.credits });

		if (!updated) throw new Error("Insufficient credits");

		await tx.insert(ledger).values({
			userId,
			type: "spend",
			amount: -amount,
			balanceAfter: updated.credits,
			toolName,
			modelId,
			idempotencyKey,
			meta,
		});

		return { balanceAfter: updated.credits, applied: true };
	});
}

/** Deduct credits from chat pool and record in ledger (atomic) */
export async function deductChatCredits(
	db: Database,
	chatId: string,
	userId: string,
	amount: number,
	toolName: string,
	modelId: string | null,
	idempotencyKey?: string,
	meta?: Record<string, unknown>,
): Promise<IdempotentCreditResult> {
	return db.transaction(async (tx) => {
		if (idempotencyKey) {
			const [inserted] = await tx
				.insert(ledger)
				.values({
					userId,
					chatId,
					type: "spend",
					amount: -amount,
					balanceAfter: 0,
					toolName,
					modelId,
					idempotencyKey,
					meta,
				})
				.onConflictDoNothing({ target: ledger.idempotencyKey })
				.returning({ id: ledger.id });

			if (!inserted) {
				const [existing] = await tx
					.select({ balanceAfter: ledger.balanceAfter })
					.from(ledger)
					.where(eq(ledger.idempotencyKey, idempotencyKey))
					.limit(1);
				return { balanceAfter: existing?.balanceAfter ?? 0, applied: false };
			}

			const [updated] = await tx
				.update(chats)
				.set({
					credits: sql`${chats.credits} - ${amount}`,
				})
				.where(and(eq(chats.id, chatId), sql`${chats.credits} >= ${amount}`))
				.returning({ credits: chats.credits });

			if (!updated) throw new Error("Insufficient chat credits");

			await tx
				.update(ledger)
				.set({ balanceAfter: updated.credits })
				.where(eq(ledger.id, inserted.id));

			return { balanceAfter: updated.credits, applied: true };
		}

		const [updated] = await tx
			.update(chats)
			.set({
				credits: sql`${chats.credits} - ${amount}`,
			})
			.where(and(eq(chats.id, chatId), sql`${chats.credits} >= ${amount}`))
			.returning({ credits: chats.credits });

		if (!updated) throw new Error("Insufficient chat credits");

		await tx.insert(ledger).values({
			userId,
			chatId,
			type: "spend",
			amount: -amount,
			balanceAfter: updated.credits,
			toolName,
			modelId,
			idempotencyKey,
			meta,
		});

		return { balanceAfter: updated.credits, applied: true };
	});
}

/** Add credits to user and record in ledger (atomic, idempotent) */
export async function addUserCreditsWithResult(
	db: Database,
	userId: string,
	amount: number,
	type: string,
	telegramChargeId?: string,
	idempotencyKey?: string,
	meta?: Record<string, unknown>,
): Promise<IdempotentCreditResult> {
	return db.transaction(async (tx) => {
		if (idempotencyKey) {
			const [inserted] = await tx
				.insert(ledger)
				.values({
					userId,
					type,
					amount,
					balanceAfter: 0,
					telegramChargeId,
					idempotencyKey,
					meta,
				})
				.onConflictDoNothing({ target: ledger.idempotencyKey })
				.returning({ id: ledger.id });

			if (!inserted) {
				const [existing] = await tx
					.select({ balanceAfter: ledger.balanceAfter })
					.from(ledger)
					.where(eq(ledger.idempotencyKey, idempotencyKey))
					.limit(1);
				return { balanceAfter: existing?.balanceAfter ?? 0, applied: false };
			}

			const [updated] = await tx
				.update(users)
				.set({
					credits: sql`${users.credits} + ${amount}`,
				})
				.where(eq(users.id, userId))
				.returning({ credits: users.credits });

			if (!updated) throw new Error("User not found");

			await tx
				.update(ledger)
				.set({ balanceAfter: updated.credits })
				.where(eq(ledger.id, inserted.id));

			return { balanceAfter: updated.credits, applied: true };
		}

		const [updated] = await tx
			.update(users)
			.set({
				credits: sql`${users.credits} + ${amount}`,
			})
			.where(eq(users.id, userId))
			.returning({ credits: users.credits });

		if (!updated) throw new Error("User not found");

		await tx.insert(ledger).values({
			userId,
			type,
			amount,
			balanceAfter: updated.credits,
			telegramChargeId,
			idempotencyKey,
			meta,
		});

		return { balanceAfter: updated.credits, applied: true };
	});
}

/** Add credits to user and record in ledger (atomic, idempotent) */
export async function addUserCredits(
	db: Database,
	userId: string,
	amount: number,
	type: string,
	telegramChargeId?: string,
	idempotencyKey?: string,
	meta?: Record<string, unknown>,
): Promise<number> {
	const result = await addUserCreditsWithResult(
		db,
		userId,
		amount,
		type,
		telegramChargeId,
		idempotencyKey,
		meta,
	);
	return result.balanceAfter;
}

/** Add credits to chat pool and record in ledger (atomic, idempotent) */
export async function addChatCreditsWithResult(
	db: Database,
	chatId: string,
	userId: string,
	amount: number,
	type: string,
	telegramChargeId?: string,
	idempotencyKey?: string,
	meta?: Record<string, unknown>,
): Promise<IdempotentCreditResult> {
	return db.transaction(async (tx) => {
		if (idempotencyKey) {
			const [inserted] = await tx
				.insert(ledger)
				.values({
					userId,
					chatId,
					type,
					amount,
					balanceAfter: 0,
					telegramChargeId,
					idempotencyKey,
					meta,
				})
				.onConflictDoNothing({ target: ledger.idempotencyKey })
				.returning({ id: ledger.id });

			if (!inserted) {
				const [existing] = await tx
					.select({ balanceAfter: ledger.balanceAfter })
					.from(ledger)
					.where(eq(ledger.idempotencyKey, idempotencyKey))
					.limit(1);
				return { balanceAfter: existing?.balanceAfter ?? 0, applied: false };
			}

			const [updated] = await tx
				.update(chats)
				.set({
					credits: sql`${chats.credits} + ${amount}`,
				})
				.where(eq(chats.id, chatId))
				.returning({ credits: chats.credits });

			if (!updated) throw new Error("Chat not found");

			await tx
				.update(ledger)
				.set({ balanceAfter: updated.credits })
				.where(eq(ledger.id, inserted.id));

			return { balanceAfter: updated.credits, applied: true };
		}

		const [updated] = await tx
			.update(chats)
			.set({
				credits: sql`${chats.credits} + ${amount}`,
			})
			.where(eq(chats.id, chatId))
			.returning({ credits: chats.credits });

		if (!updated) throw new Error("Chat not found");

		await tx.insert(ledger).values({
			userId,
			chatId,
			type,
			amount,
			balanceAfter: updated.credits,
			telegramChargeId,
			idempotencyKey,
			meta,
		});

		return { balanceAfter: updated.credits, applied: true };
	});
}

/** Add credits to chat pool and record in ledger (atomic, idempotent) */
export async function addChatCredits(
	db: Database,
	chatId: string,
	userId: string,
	amount: number,
	type: string,
	telegramChargeId?: string,
	idempotencyKey?: string,
	meta?: Record<string, unknown>,
): Promise<number> {
	const result = await addChatCreditsWithResult(
		db,
		chatId,
		userId,
		amount,
		type,
		telegramChargeId,
		idempotencyKey,
		meta,
	);
	return result.balanceAfter;
}

export async function applyUserPackPayment(
	db: Database,
	record: StarsPaymentRecord,
): Promise<IdempotentCreditResult> {
	const idempotencyKey = `pack:${record.telegramChargeId}`;

	return db.transaction(async (tx) => {
		const receipt = await recordPaymentReceiptIn(tx, record);
		if (!receipt.needsSettlement) {
			const [existing] = await tx
				.select({ balanceAfter: ledger.balanceAfter })
				.from(ledger)
				.where(eq(ledger.idempotencyKey, idempotencyKey))
				.limit(1);
			return { balanceAfter: existing?.balanceAfter ?? 0, applied: false };
		}

		const debtSettlement = await settleOpenDebts(tx, {
			userId: record.userId,
			chatId: null,
			amount: record.credits,
			paymentReceiptId: receipt.id,
			meta: { telegramChargeId: record.telegramChargeId },
		});
		const spendableCredits = debtSettlement.remainingAmount;

		const [updated] = await tx
			.update(users)
			.set({ credits: sql`${users.credits} + ${spendableCredits}` })
			.where(eq(users.id, record.userId))
			.returning({ credits: users.credits });

		if (!updated) throw new Error("User not found");

		await tx.insert(ledger).values({
			userId: record.userId,
			type: "purchase",
			amount: spendableCredits,
			balanceAfter: updated.credits,
			telegramChargeId: record.telegramChargeId,
			idempotencyKey,
			meta: {
				paymentReceiptId: receipt.id,
				purchasedCredits: record.credits,
				debtRecovered: debtSettlement.settledAmount,
			},
		});
		await markPaymentSettledIn(tx, receipt.id);

		return { balanceAfter: updated.credits, applied: true };
	});
}

export async function applyChatPackPayment(
	db: Database,
	record: StarsPaymentRecord,
): Promise<IdempotentCreditResult> {
	if (!record.chatId) throw new Error("Chat payment missing chat ID");
	const chatId = record.chatId;
	const idempotencyKey = `pack:${record.telegramChargeId}`;

	return db.transaction(async (tx) => {
		const receipt = await recordPaymentReceiptIn(tx, record);
		if (!receipt.needsSettlement) {
			const [existing] = await tx
				.select({ balanceAfter: ledger.balanceAfter })
				.from(ledger)
				.where(eq(ledger.idempotencyKey, idempotencyKey))
				.limit(1);
			return { balanceAfter: existing?.balanceAfter ?? 0, applied: false };
		}

		const debtSettlement = await settleOpenDebts(tx, {
			userId: record.userId,
			chatId,
			amount: record.credits,
			paymentReceiptId: receipt.id,
			meta: { telegramChargeId: record.telegramChargeId },
		});
		const spendableCredits = debtSettlement.remainingAmount;

		const [updated] = await tx
			.update(chats)
			.set({ credits: sql`${chats.credits} + ${spendableCredits}` })
			.where(eq(chats.id, chatId))
			.returning({ credits: chats.credits });

		if (!updated) throw new Error("Chat not found");

		await tx.insert(ledger).values({
			userId: record.userId,
			chatId,
			type: "purchase",
			amount: spendableCredits,
			balanceAfter: updated.credits,
			telegramChargeId: record.telegramChargeId,
			idempotencyKey,
			meta: {
				paymentReceiptId: receipt.id,
				purchasedCredits: record.credits,
				debtRecovered: debtSettlement.settledAmount,
			},
		});
		await markPaymentSettledIn(tx, receipt.id);

		return { balanceAfter: updated.credits, applied: true };
	});
}

/** Record an idempotent free tool use and increment its daily quota. */
export async function recordFreeToolUsage(
	db: Database,
	userId: string,
	chatId: string,
	toolName: string,
	modelId: string | null,
	freeDailyLimit: number,
	idempotencyKey?: string,
	meta?: Record<string, unknown>,
): Promise<ToolDebitResult> {
	if (!idempotencyKey) {
		try {
			await reserveDailyUsageIn(db, userId, chatId, toolName, freeDailyLimit);
			return "applied";
		} catch (err) {
			if (err instanceof QuotaExhaustedError) return "quota_exhausted";
			throw err;
		}
	}

	try {
		return await db.transaction(async (tx) => {
			const [inserted] = await tx
				.insert(ledger)
				.values({
					userId,
					chatId,
					type: "spend",
					amount: 0,
					balanceAfter: 0,
					toolName,
					modelId,
					idempotencyKey,
					meta,
				})
				.onConflictDoNothing({ target: ledger.idempotencyKey })
				.returning({ id: ledger.id });

			if (!inserted) return "duplicate";

			await reserveDailyUsageIn(tx, userId, chatId, toolName, freeDailyLimit);

			const [userRow] = await tx
				.select({ credits: users.credits })
				.from(users)
				.where(eq(users.id, userId))
				.limit(1);

			await tx
				.update(ledger)
				.set({ balanceAfter: userRow?.credits ?? 0 })
				.where(eq(ledger.id, inserted.id));

			return "applied";
		});
	} catch (err) {
		if (err instanceof QuotaExhaustedError) return "quota_exhausted";
		throw err;
	}
}

/** Apply a subscription payment and update subscription status atomically. */
export async function applySubscriptionPayment(
	db: Database,
	userId: string,
	amount: number,
	planId: string,
	telegramChargeId: string,
	subscriptionExpiresAt: Date,
	payment: Omit<
		StarsPaymentRecord,
		| "userId"
		| "telegramChargeId"
		| "productType"
		| "productId"
		| "creditTarget"
		| "credits"
	>,
	meta?: Record<string, unknown>,
): Promise<IdempotentCreditResult> {
	const idempotencyKey = `sub:${telegramChargeId}`;
	const paymentMeta = {
		...meta,
		planId,
		subscriptionExpiresAt: subscriptionExpiresAt.toISOString(),
	};

	return db.transaction(async (tx) => {
		const receipt = await recordPaymentReceiptIn(tx, {
			...payment,
			userId,
			telegramChargeId,
			productType: "subscription",
			productId: planId,
			creditTarget: "user",
			credits: amount,
			meta: paymentMeta,
		});

		if (!receipt.needsSettlement) {
			const [existing] = await tx
				.select({
					balanceAfter: ledger.balanceAfter,
					meta: ledger.meta,
				})
				.from(ledger)
				.where(eq(ledger.idempotencyKey, idempotencyKey))
				.limit(1);

			await recomputeActiveSubscriptionIn(tx, userId);

			return { balanceAfter: existing?.balanceAfter ?? 0, applied: false };
		}

		const debtSettlement = await settleOpenDebts(tx, {
			userId,
			chatId: null,
			amount,
			paymentReceiptId: receipt.id,
			meta: { telegramChargeId },
		});
		const spendableCredits = debtSettlement.remainingAmount;

		const [inserted] = await tx
			.insert(ledger)
			.values({
				userId,
				type: "subscription",
				amount: spendableCredits,
				balanceAfter: 0,
				telegramChargeId,
				idempotencyKey,
				meta: {
					...paymentMeta,
					purchasedCredits: amount,
					debtRecovered: debtSettlement.settledAmount,
				},
			})
			.onConflictDoNothing({ target: ledger.idempotencyKey })
			.returning({ id: ledger.id });

		if (!inserted) throw new Error("Subscription ledger conflict");

		const [updated] = await tx
			.update(users)
			.set({
				credits: sql`${users.credits} + ${spendableCredits}`,
			})
			.where(eq(users.id, userId))
			.returning({ credits: users.credits });

		if (!updated) throw new Error("User not found");

		await tx
			.update(ledger)
			.set({ balanceAfter: updated.credits })
			.where(eq(ledger.id, inserted.id));

		await tx
			.insert(subscriptionPeriods)
			.values({
				userId,
				paymentId: receipt.id,
				telegramChargeId,
				planId,
				credits: amount,
				expiresAt: subscriptionExpiresAt,
				meta: paymentMeta,
			})
			.onConflictDoNothing({
				target: subscriptionPeriods.telegramChargeId,
			});

		await recomputeActiveSubscriptionIn(tx, userId);
		await markPaymentSettledIn(tx, receipt.id);

		return { balanceAfter: updated.credits, applied: true };
	});
}

async function recomputeActiveSubscriptionIn(
	tx: CreditTransaction,
	userId: string,
): Promise<void> {
	const [active] = await tx
		.select({
			planId: subscriptionPeriods.planId,
			expiresAt: subscriptionPeriods.expiresAt,
		})
		.from(subscriptionPeriods)
		.where(
			and(
				eq(subscriptionPeriods.userId, userId),
				eq(subscriptionPeriods.status, "active"),
				sql`${subscriptionPeriods.expiresAt} > now()`,
			),
		)
		.orderBy(
			desc(subscriptionPeriods.expiresAt),
			desc(subscriptionPeriods.createdAt),
		)
		.limit(1);

	await tx
		.update(users)
		.set({
			subscriptionTier: active?.planId ?? null,
			subscriptionExpiresAt: active?.expiresAt ?? null,
		})
		.where(eq(users.id, userId));
}

async function debitUserCreditsForRefund(
	tx: CreditTransaction,
	userId: string,
	amount: number,
): Promise<{ balanceAfter: number; recoveredAmount: number }> {
	const [current] = await tx
		.select({ credits: users.credits })
		.from(users)
		.where(eq(users.id, userId))
		.for("update")
		.limit(1);

	if (!current) throw new Error("User not found");

	const recoveredAmount = Math.min(current.credits, amount);
	const balanceAfter = current.credits - recoveredAmount;

	await tx
		.update(users)
		.set({ credits: balanceAfter })
		.where(eq(users.id, userId));

	return {
		balanceAfter,
		recoveredAmount,
	};
}

async function debitChatCreditsForRefund(
	tx: CreditTransaction,
	chatId: string,
	amount: number,
): Promise<{ balanceAfter: number; recoveredAmount: number }> {
	const [current] = await tx
		.select({ credits: chats.credits })
		.from(chats)
		.where(eq(chats.id, chatId))
		.for("update")
		.limit(1);

	if (!current) throw new Error("Chat not found");

	const recoveredAmount = Math.min(current.credits, amount);
	const balanceAfter = current.credits - recoveredAmount;

	await tx
		.update(chats)
		.set({ credits: balanceAfter })
		.where(eq(chats.id, chatId));

	return {
		balanceAfter,
		recoveredAmount,
	};
}

/** Reconcile a successful Telegram Stars refund against local credit balances. */
export async function reconcileStarRefund(
	db: Database,
	telegramChargeId: string,
	meta?: Record<string, unknown>,
): Promise<RefundReconciliationResult> {
	const refundKey = `refund:${telegramChargeId}`;

	return db.transaction(async (tx) => {
		const [original] = await tx
			.select()
			.from(ledger)
			.where(
				and(
					eq(ledger.telegramChargeId, telegramChargeId),
					sql`${ledger.amount} > 0`,
					sql`${ledger.type} IN ('purchase', 'subscription')`,
				),
			)
			.orderBy(desc(ledger.createdAt))
			.limit(1);

		if (!original) {
			const [receipt] = await tx
				.select()
				.from(paymentReceipts)
				.where(eq(paymentReceipts.telegramChargeId, telegramChargeId))
				.limit(1);

			if (!receipt) {
				throw new Error("No local payment found for charge");
			}

			const [inserted] = await tx
				.insert(ledger)
				.values({
					userId: receipt.userId,
					chatId: receipt.chatId,
					type: "refund",
					amount: 0,
					balanceAfter: 0,
					telegramChargeId,
					idempotencyKey: refundKey,
					meta: {
						...meta,
						paymentReceiptId: receipt.id,
						originalType: receipt.productType,
						originalAmount: receipt.credits,
						originalStars: receipt.stars,
						recoveredAmount: 0,
						unrecoveredAmount: receipt.credits,
					},
				})
				.onConflictDoNothing({ target: ledger.idempotencyKey })
				.returning({ id: ledger.id });

			if (inserted) {
				await tx
					.update(paymentReceipts)
					.set({ status: "refunded", refundedAt: new Date() })
					.where(eq(paymentReceipts.id, receipt.id));
				if (receipt.credits > 0) {
					await createCreditDebt(tx, {
						userId: receipt.userId,
						chatId: receipt.chatId,
						paymentReceiptId: receipt.id,
						telegramChargeId,
						target: receipt.chatId ? "chat" : "user",
						amount: receipt.credits,
						meta: {
							...meta,
							source: "refund_unrecovered",
							originalType: receipt.productType,
						},
					});
				}
			}

			return {
				applied: Boolean(inserted),
				target: receipt.chatId ? "chat" : "user",
				originalType: receipt.productType,
				originalAmount: receipt.credits,
				recoveredAmount: 0,
				unrecoveredAmount: receipt.credits,
				balanceAfter: 0,
			};
		}

		const [inserted] = await tx
			.insert(ledger)
			.values({
				userId: original.userId,
				chatId: original.chatId,
				type: "refund",
				amount: 0,
				balanceAfter: 0,
				telegramChargeId,
				idempotencyKey: refundKey,
				meta: {
					...meta,
					originalLedgerId: original.id,
					originalType: original.type,
					originalAmount: original.amount,
				},
			})
			.onConflictDoNothing({ target: ledger.idempotencyKey })
			.returning({ id: ledger.id });

		if (!inserted) {
			const [existing] = await tx
				.select({
					amount: ledger.amount,
					balanceAfter: ledger.balanceAfter,
					meta: ledger.meta,
				})
				.from(ledger)
				.where(eq(ledger.idempotencyKey, refundKey))
				.limit(1);

			const recoveredAmount =
				typeof existing?.meta?.recoveredAmount === "number"
					? existing.meta.recoveredAmount
					: Math.abs(existing?.amount ?? 0);
			const originalAmount =
				typeof existing?.meta?.originalAmount === "number"
					? existing.meta.originalAmount
					: original.amount;

			return {
				applied: false,
				target: original.chatId ? "chat" : "user",
				originalType: original.type,
				originalAmount,
				recoveredAmount,
				unrecoveredAmount: Math.max(0, originalAmount - recoveredAmount),
				balanceAfter: existing?.balanceAfter ?? 0,
			};
		}

		const debit = original.chatId
			? await debitChatCreditsForRefund(tx, original.chatId, original.amount)
			: await debitUserCreditsForRefund(tx, original.userId, original.amount);
		const unrecoveredAmount = Math.max(
			0,
			original.amount - debit.recoveredAmount,
		);

		await tx
			.update(ledger)
			.set({
				amount: -debit.recoveredAmount,
				balanceAfter: debit.balanceAfter,
				meta: {
					...meta,
					originalLedgerId: original.id,
					originalType: original.type,
					originalAmount: original.amount,
					recoveredAmount: debit.recoveredAmount,
					unrecoveredAmount,
				},
			})
			.where(eq(ledger.id, inserted.id));

		await tx
			.update(paymentReceipts)
			.set({ status: "refunded", refundedAt: new Date() })
			.where(eq(paymentReceipts.telegramChargeId, telegramChargeId));

		if (original.type === "subscription") {
			await tx
				.update(subscriptionPeriods)
				.set({ status: "refunded", refundedAt: new Date() })
				.where(eq(subscriptionPeriods.telegramChargeId, telegramChargeId));
			await recomputeActiveSubscriptionIn(tx, original.userId);
		}

		if (unrecoveredAmount > 0) {
			const [receipt] = await tx
				.select({ id: paymentReceipts.id })
				.from(paymentReceipts)
				.where(eq(paymentReceipts.telegramChargeId, telegramChargeId))
				.limit(1);
			await createCreditDebt(tx, {
				userId: original.userId,
				chatId: original.chatId,
				paymentReceiptId: receipt?.id ?? null,
				telegramChargeId,
				target: original.chatId ? "chat" : "user",
				amount: unrecoveredAmount,
				meta: {
					...meta,
					source: "refund_unrecovered",
					originalLedgerId: original.id,
					originalType: original.type,
					originalAmount: original.amount,
				},
			});
		}

		return {
			applied: true,
			target: original.chatId ? "chat" : "user",
			originalType: original.type,
			originalAmount: original.amount,
			recoveredAmount: debit.recoveredAmount,
			unrecoveredAmount,
			balanceAfter: debit.balanceAfter,
		};
	});
}

/** Find a transaction by idempotency key (for dedup) */
export async function getTransactionByIdempotencyKey(
	db: Database,
	key: string,
): Promise<typeof ledger.$inferSelect | null> {
	const [row] = await db
		.select()
		.from(ledger)
		.where(eq(ledger.idempotencyKey, key))
		.limit(1);
	return row ?? null;
}
