import { eq } from "drizzle-orm";
import type { User as TelegramUser } from "grammy/types";
import type { Database } from "../connection";
import { users } from "../schema";

/** Upsert a Telegram user → DB user, returns the DB row */
export async function upsertUser(
	db: Database,
	tgUser: TelegramUser,
): Promise<typeof users.$inferSelect> {
	const [row] = await db
		.insert(users)
		.values({
			telegramId: tgUser.id,
			isBot: tgUser.is_bot,
			firstName: tgUser.first_name,
			lastName: tgUser.last_name ?? null,
			username: tgUser.username ?? null,
			languageCode: tgUser.language_code ?? null,
			isPremium: tgUser.is_premium ?? false,
		})
		.onConflictDoUpdate({
			target: users.telegramId,
			set: {
				firstName: tgUser.first_name,
				lastName: tgUser.last_name ?? null,
				username: tgUser.username ?? null,
				languageCode: tgUser.language_code ?? null,
				isPremium: tgUser.is_premium ?? false,
			},
		})
		.returning();

	return row!;
}

/** Get a user by Telegram ID */
export async function getUserByTelegramId(
	db: Database,
	telegramId: number,
): Promise<typeof users.$inferSelect | null> {
	const [row] = await db
		.select()
		.from(users)
		.where(eq(users.telegramId, telegramId))
		.limit(1);
	return row ?? null;
}
