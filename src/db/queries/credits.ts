import { and, eq, sql } from "drizzle-orm";
import type { Database } from "../connection";
import { chats, ledger, usageQuotas, users } from "../schema";

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

/** Increment daily usage for a tool */
export async function incrementDailyUsage(
	db: Database,
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
export async function addUserCredits(
	db: Database,
	userId: string,
	amount: number,
	type: string,
	telegramChargeId?: string,
	idempotencyKey?: string,
	meta?: Record<string, unknown>,
): Promise<number> {
	return db.transaction(async (tx) => {
		// Idempotency check
		if (idempotencyKey) {
			const [existing] = await tx
				.select()
				.from(ledger)
				.where(eq(ledger.idempotencyKey, idempotencyKey))
				.limit(1);
			if (existing) return existing.balanceAfter;
		}

		const [updated] = await tx
			.update(users)
			.set({
				credits: sql`${users.credits} + ${amount}`,
			})
			.where(eq(users.id, userId))
			.returning({ credits: users.credits });

		const newBalance = updated?.credits ?? 0;

		await tx.insert(ledger).values({
			userId,
			type,
			amount,
			balanceAfter: newBalance,
			telegramChargeId,
			idempotencyKey,
			meta,
		});

		return newBalance;
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
	return db.transaction(async (tx) => {
		// Idempotency check
		if (idempotencyKey) {
			const [existing] = await tx
				.select()
				.from(ledger)
				.where(eq(ledger.idempotencyKey, idempotencyKey))
				.limit(1);
			if (existing) return existing.balanceAfter;
		}

		const [updated] = await tx
			.update(chats)
			.set({
				credits: sql`${chats.credits} + ${amount}`,
			})
			.where(eq(chats.id, chatId))
			.returning({ credits: chats.credits });

		const newBalance = updated?.credits ?? 0;

		await tx.insert(ledger).values({
			userId,
			chatId,
			type,
			amount,
			balanceAfter: newBalance,
			telegramChargeId,
			idempotencyKey,
			meta,
		});

		return newBalance;
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
