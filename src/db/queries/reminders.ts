import { and, eq, lte, sql } from "drizzle-orm";
import type { Database } from "../connection";
import { reminders } from "../schema";

/** Create a new reminder */
export async function createReminder(
	db: Database,
	data: {
		chatId: string;
		userId: string;
		threadId?: number | null;
		description: string;
		message?: string | null;
		prompt?: string | null;
		usesLlm?: boolean;
		fireAt?: Date | null;
		cronExpression?: string | null;
		isRecurring?: boolean;
		replyToMessageId?: number | null;
	},
): Promise<typeof reminders.$inferSelect> {
	const [row] = await db
		.insert(reminders)
		.values({
			chatId: data.chatId,
			userId: data.userId,
			threadId: data.threadId ?? null,
			description: data.description,
			message: data.message ?? null,
			prompt: data.prompt ?? null,
			usesLlm: data.usesLlm ?? false,
			fireAt: data.fireAt ?? null,
			cronExpression: data.cronExpression ?? null,
			isRecurring: data.isRecurring ?? false,
			replyToMessageId: data.replyToMessageId ?? null,
		})
		.returning();

	return row!;
}

/** Get all reminders that are due to fire */
export async function getDueReminders(
	db: Database,
): Promise<(typeof reminders.$inferSelect)[]> {
	return db
		.select()
		.from(reminders)
		.where(
			and(eq(reminders.status, "active"), lte(reminders.fireAt, new Date())),
		);
}

/** Get reminders for a specific chat, optionally filtered by user */
export async function getRemindersForChat(
	db: Database,
	chatId: string,
	userId?: string,
): Promise<(typeof reminders.$inferSelect)[]> {
	const conditions = [
		eq(reminders.chatId, chatId),
		eq(reminders.status, "active"),
	];

	if (userId) {
		conditions.push(eq(reminders.userId, userId));
	}

	return db
		.select()
		.from(reminders)
		.where(and(...conditions));
}

/** Cancel a reminder */
export async function cancelReminder(db: Database, id: string): Promise<void> {
	await db
		.update(reminders)
		.set({ status: "cancelled" })
		.where(eq(reminders.id, id));
}

/** Mark a one-time reminder as completed */
export async function markReminderCompleted(
	db: Database,
	id: string,
): Promise<void> {
	await db
		.update(reminders)
		.set({
			status: "completed",
			lastFiredAt: new Date(),
			fireCount: sql`${reminders.fireCount} + 1`,
		})
		.where(eq(reminders.id, id));
}

/** Update the next fire time for a recurring reminder */
export async function updateNextFireAt(
	db: Database,
	id: string,
	nextFireAt: Date,
): Promise<void> {
	await db
		.update(reminders)
		.set({
			fireAt: nextFireAt,
			lastFiredAt: new Date(),
			fireCount: sql`${reminders.fireCount} + 1`,
		})
		.where(eq(reminders.id, id));
}

/** Mark a reminder as failed */
export async function markReminderFailed(
	db: Database,
	id: string,
	error: string,
): Promise<void> {
	await db
		.update(reminders)
		.set({
			status: "failed",
			meta: { error, failedAt: new Date().toISOString() },
		})
		.where(eq(reminders.id, id));
}

/** Get a reminder by ID */
export async function getReminderById(
	db: Database,
	id: string,
): Promise<typeof reminders.$inferSelect | null> {
	const [row] = await db
		.select()
		.from(reminders)
		.where(eq(reminders.id, id))
		.limit(1);
	return row ?? null;
}

/** Count active reminders for a user in a chat */
export async function countActiveReminders(
	db: Database,
	userId: string,
	chatId: string,
): Promise<number> {
	const rows = await db
		.select({ count: sql<number>`count(*)::int` })
		.from(reminders)
		.where(
			and(
				eq(reminders.userId, userId),
				eq(reminders.chatId, chatId),
				eq(reminders.status, "active"),
			),
		);
	return rows[0]?.count ?? 0;
}

/** Count active recurring reminders for a user across all chats */
export async function countRecurringReminders(
	db: Database,
	userId: string,
): Promise<number> {
	const rows = await db
		.select({ count: sql<number>`count(*)::int` })
		.from(reminders)
		.where(
			and(
				eq(reminders.userId, userId),
				eq(reminders.status, "active"),
				eq(reminders.isRecurring, true),
			),
		);
	return rows[0]?.count ?? 0;
}
