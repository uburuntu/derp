import { eq } from "drizzle-orm";
import type { Chat as TelegramChat } from "grammy/types";
import type { Database } from "../connection";
import { type ChatSettings, chats } from "../schema";

/** Upsert a Telegram chat → DB chat, returns the DB row */
export async function upsertChat(
	db: Database,
	tgChat: TelegramChat,
): Promise<typeof chats.$inferSelect> {
	const isGroup = tgChat.type === "group" || tgChat.type === "supergroup";
	const defaultSettings: ChatSettings = {
		memoryAccess: isGroup ? "admins" : "everyone",
		remindersAccess: isGroup ? "admins" : "everyone",
	};

	const [row] = await db
		.insert(chats)
		.values({
			telegramId: tgChat.id,
			type: tgChat.type,
			title: "title" in tgChat ? (tgChat.title ?? null) : null,
			username: "username" in tgChat ? (tgChat.username ?? null) : null,
			firstName: "first_name" in tgChat ? (tgChat.first_name ?? null) : null,
			lastName: "last_name" in tgChat ? (tgChat.last_name ?? null) : null,
			isForum: "is_forum" in tgChat ? (tgChat.is_forum ?? false) : false,
			settings: defaultSettings,
		})
		.onConflictDoUpdate({
			target: chats.telegramId,
			set: {
				type: tgChat.type,
				title: "title" in tgChat ? (tgChat.title ?? null) : null,
				username: "username" in tgChat ? (tgChat.username ?? null) : null,
				firstName: "first_name" in tgChat ? (tgChat.first_name ?? null) : null,
				lastName: "last_name" in tgChat ? (tgChat.last_name ?? null) : null,
				isForum: "is_forum" in tgChat ? (tgChat.is_forum ?? false) : false,
			},
		})
		.returning();

	return row!;
}

/** Get a chat by Telegram ID */
export async function getChatByTelegramId(
	db: Database,
	telegramId: number,
): Promise<typeof chats.$inferSelect | null> {
	const [row] = await db
		.select()
		.from(chats)
		.where(eq(chats.telegramId, telegramId))
		.limit(1);
	return row ?? null;
}

/** Update chat memory */
export async function updateChatMemory(
	db: Database,
	chatId: string,
	memory: string | null,
): Promise<void> {
	await db.update(chats).set({ memory }).where(eq(chats.id, chatId));
}

/** Update chat settings */
export async function updateChatSettings(
	db: Database,
	chatId: string,
	settings: Partial<ChatSettings>,
): Promise<void> {
	const [current] = await db
		.select({ settings: chats.settings })
		.from(chats)
		.where(eq(chats.id, chatId))
		.limit(1);

	const merged = { ...(current?.settings ?? {}), ...settings } as ChatSettings;
	await db.update(chats).set({ settings: merged }).where(eq(chats.id, chatId));
}

/** Update chat personality */
export async function updateChatPersonality(
	db: Database,
	chatId: string,
	personality: string,
	customSystemPrompt?: string | null,
): Promise<void> {
	await db
		.update(chats)
		.set({ personality, customPrompt: customSystemPrompt ?? null })
		.where(eq(chats.id, chatId));
}
