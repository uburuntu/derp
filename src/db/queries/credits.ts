import { and, desc, eq, sql } from "drizzle-orm";
import type { Database } from "../connection";
import { chats, ledger, usageQuotas, users } from "../schema";

type CreditTransaction = Parameters<Parameters<Database["transaction"]>[0]>[0];

export interface IdempotentCreditResult {
	balanceAfter: number;
	applied: boolean;
}

export interface CreditTransferResult {
	userCredits: number;
	chatCredits: number;
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
): Promise<number> {
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
				return existing?.balanceAfter ?? 0;
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

			return updated.credits;
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

		return updated.credits;
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
): Promise<number> {
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
				return existing?.balanceAfter ?? 0;
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

			return updated.credits;
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

		return updated.credits;
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

/** Record an idempotent free tool use and increment its daily quota. */
export async function recordFreeToolUsage(
	db: Database,
	userId: string,
	chatId: string,
	toolName: string,
	modelId: string | null,
	idempotencyKey?: string,
	meta?: Record<string, unknown>,
): Promise<boolean> {
	if (!idempotencyKey) {
		await incrementDailyUsage(db, userId, chatId, toolName);
		return true;
	}

	return db.transaction(async (tx) => {
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

		if (!inserted) return false;

		const [userRow] = await tx
			.select({ credits: users.credits })
			.from(users)
			.where(eq(users.id, userId))
			.limit(1);

		await tx
			.update(ledger)
			.set({ balanceAfter: userRow?.credits ?? 0 })
			.where(eq(ledger.id, inserted.id));

		await incrementDailyUsageIn(tx, userId, chatId, toolName);
		return true;
	});
}

/** Move credits from a user's balance into a chat pool in one transaction. */
export async function transferUserCreditsToChat(
	db: Database,
	userId: string,
	chatId: string,
	amount: number,
	idempotencyKey?: string,
	meta?: Record<string, unknown>,
): Promise<CreditTransferResult> {
	return db.transaction(async (tx) => {
		let transferLedgerId: string | null = null;
		if (idempotencyKey) {
			const [inserted] = await tx
				.insert(ledger)
				.values({
					userId,
					chatId,
					type: "transfer",
					amount: -amount,
					balanceAfter: 0,
					idempotencyKey,
					meta,
				})
				.onConflictDoNothing({ target: ledger.idempotencyKey })
				.returning({ id: ledger.id });

			if (!inserted) {
				const [existing] = await tx
					.select({
						balanceAfter: ledger.balanceAfter,
						meta: ledger.meta,
					})
					.from(ledger)
					.where(eq(ledger.idempotencyKey, idempotencyKey))
					.limit(1);
				const chatCredits =
					typeof existing?.meta?.chatBalanceAfter === "number"
						? existing.meta.chatBalanceAfter
						: 0;
				return {
					userCredits: existing?.balanceAfter ?? 0,
					chatCredits,
					applied: false,
				};
			}
			transferLedgerId = inserted.id;
		}

		const [updatedUser] = await tx
			.update(users)
			.set({
				credits: sql`${users.credits} - ${amount}`,
			})
			.where(and(eq(users.id, userId), sql`${users.credits} >= ${amount}`))
			.returning({ credits: users.credits });

		if (!updatedUser) throw new Error("Insufficient credits");

		const [updatedChat] = await tx
			.update(chats)
			.set({
				credits: sql`${chats.credits} + ${amount}`,
			})
			.where(eq(chats.id, chatId))
			.returning({ credits: chats.credits });

		if (!updatedChat) throw new Error("Chat not found");

		const transferMeta = {
			...meta,
			chatBalanceAfter: updatedChat.credits,
		};

		if (transferLedgerId) {
			await tx
				.update(ledger)
				.set({
					balanceAfter: updatedUser.credits,
					meta: transferMeta,
				})
				.where(eq(ledger.id, transferLedgerId));
		} else {
			await tx.insert(ledger).values({
				userId,
				chatId,
				type: "transfer",
				amount: -amount,
				balanceAfter: updatedUser.credits,
				meta: transferMeta,
			});
		}

		await tx
			.insert(ledger)
			.values({
				userId,
				chatId,
				type: "transfer",
				amount,
				balanceAfter: updatedChat.credits,
				idempotencyKey: idempotencyKey ? `${idempotencyKey}:chat` : undefined,
				meta: {
					...meta,
					userBalanceAfter: updatedUser.credits,
				},
			})
			.onConflictDoNothing({ target: ledger.idempotencyKey });

		return {
			userCredits: updatedUser.credits,
			chatCredits: updatedChat.credits,
			applied: true,
		};
	});
}

/** Apply a subscription payment and update subscription status atomically. */
export async function applySubscriptionPayment(
	db: Database,
	userId: string,
	amount: number,
	planId: string,
	telegramChargeId: string,
	subscriptionExpiresAt: Date,
	meta?: Record<string, unknown>,
): Promise<IdempotentCreditResult> {
	const idempotencyKey = `sub:${telegramChargeId}`;
	const paymentMeta = {
		...meta,
		planId,
		subscriptionExpiresAt: subscriptionExpiresAt.toISOString(),
	};

	return db.transaction(async (tx) => {
		const [inserted] = await tx
			.insert(ledger)
			.values({
				userId,
				type: "subscription",
				amount,
				balanceAfter: 0,
				telegramChargeId,
				idempotencyKey,
				meta: paymentMeta,
			})
			.onConflictDoNothing({ target: ledger.idempotencyKey })
			.returning({ id: ledger.id });

		if (!inserted) {
			const [existing] = await tx
				.select({
					balanceAfter: ledger.balanceAfter,
					meta: ledger.meta,
				})
				.from(ledger)
				.where(eq(ledger.idempotencyKey, idempotencyKey))
				.limit(1);

			const existingExpiryText = existing?.meta?.subscriptionExpiresAt;
			const existingExpiry =
				typeof existingExpiryText === "string"
					? new Date(existingExpiryText)
					: subscriptionExpiresAt;
			if (!Number.isNaN(existingExpiry.getTime())) {
				await ensureSubscriptionExpiry(tx, userId, planId, existingExpiry);
			}

			return { balanceAfter: existing?.balanceAfter ?? 0, applied: false };
		}

		const [updated] = await tx
			.update(users)
			.set({
				credits: sql`${users.credits} + ${amount}`,
				subscriptionTier: planId,
				subscriptionExpiresAt,
			})
			.where(eq(users.id, userId))
			.returning({ credits: users.credits });

		if (!updated) throw new Error("User not found");

		await tx
			.update(ledger)
			.set({ balanceAfter: updated.credits })
			.where(eq(ledger.id, inserted.id));

		return { balanceAfter: updated.credits, applied: true };
	});
}

async function ensureSubscriptionExpiry(
	tx: CreditTransaction,
	userId: string,
	planId: string,
	expiresAt: Date,
): Promise<void> {
	const [userRow] = await tx
		.select({ subscriptionExpiresAt: users.subscriptionExpiresAt })
		.from(users)
		.where(eq(users.id, userId))
		.limit(1);

	if (
		!userRow?.subscriptionExpiresAt ||
		userRow.subscriptionExpiresAt < expiresAt
	) {
		await tx
			.update(users)
			.set({
				subscriptionTier: planId,
				subscriptionExpiresAt: expiresAt,
			})
			.where(eq(users.id, userId));
	}
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
			throw new Error("No local purchase found for charge");
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

		if (original.type === "subscription") {
			const [laterSubscription] = await tx
				.select({ id: ledger.id })
				.from(ledger)
				.where(
					and(
						eq(ledger.userId, original.userId),
						eq(ledger.type, "subscription"),
						sql`${ledger.amount} > 0`,
						sql`${ledger.createdAt} > ${original.createdAt}`,
					),
				)
				.limit(1);

			if (!laterSubscription) {
				await tx
					.update(users)
					.set({
						subscriptionTier: null,
						subscriptionExpiresAt: null,
					})
					.where(eq(users.id, original.userId));
			}
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
