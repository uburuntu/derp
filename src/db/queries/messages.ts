import { and, desc, eq, isNull } from "drizzle-orm";
import type { Database } from "../connection";
import { type MessageMetadata, messages } from "../schema";

/** Insert a message into the DB */
export async function insertMessage(
	db: Database,
	msg: {
		chatId: string;
		userId: string | null;
		telegramMessageId: number;
		threadId?: number | null;
		direction: "in" | "out";
		contentType?: string | null;
		text?: string | null;
		mediaGroupId?: string | null;
		attachmentType?: string | null;
		attachmentFileId?: string | null;
		replyToMessageId?: number | null;
		metadata?: MessageMetadata | null;
		telegramDate: Date;
	},
): Promise<typeof messages.$inferSelect> {
	const [row] = await db
		.insert(messages)
		.values(msg)
		.onConflictDoNothing()
		.returning();

	if (row) return row;

	const [existing] = await db
		.select()
		.from(messages)
		.where(
			and(
				eq(messages.chatId, msg.chatId),
				eq(messages.telegramMessageId, msg.telegramMessageId),
			),
		)
		.limit(1);
	if (!existing) {
		throw new Error(
			`Failed to insert or fetch message ${msg.telegramMessageId} in chat ${msg.chatId}`,
		);
	}
	return existing;
}

/** Get recent messages for context building */
export async function getRecentMessages(
	db: Database,
	chatId: string,
	limit: number,
	threadId?: number | null,
): Promise<(typeof messages.$inferSelect)[]> {
	const conditions = [eq(messages.chatId, chatId), isNull(messages.deletedAt)];

	if (threadId != null) {
		conditions.push(eq(messages.threadId, threadId));
	} else {
		conditions.push(isNull(messages.threadId));
	}

	const rows = await db
		.select()
		.from(messages)
		.where(and(...conditions))
		.orderBy(desc(messages.telegramDate))
		.limit(limit);

	// Return in chronological order (oldest first)
	return rows.reverse();
}

/** Update message text (for edited_message handling) */
export async function updateMessageText(
	db: Database,
	chatId: string,
	telegramMessageId: number,
	text: string,
): Promise<void> {
	await db
		.update(messages)
		.set({ text, editedAt: new Date() })
		.where(
			and(
				eq(messages.chatId, chatId),
				eq(messages.telegramMessageId, telegramMessageId),
			),
		);
}

/** Soft-delete a message */
export async function softDeleteMessage(
	db: Database,
	chatId: string,
	telegramMessageId: number,
): Promise<void> {
	await db
		.update(messages)
		.set({ deletedAt: new Date() })
		.where(
			and(
				eq(messages.chatId, chatId),
				eq(messages.telegramMessageId, telegramMessageId),
			),
		);
}

/** Get a specific message by chat + telegram message ID */
export async function getMessageByTelegramId(
	db: Database,
	chatId: string,
	telegramMessageId: number,
): Promise<typeof messages.$inferSelect | null> {
	const [row] = await db
		.select()
		.from(messages)
		.where(
			and(
				eq(messages.chatId, chatId),
				eq(messages.telegramMessageId, telegramMessageId),
			),
		)
		.limit(1);
	return row ?? null;
}
