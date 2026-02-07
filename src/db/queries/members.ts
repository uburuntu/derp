import { and, eq, inArray } from "drizzle-orm";
import type { Database } from "../connection";
import { chatMembers, users } from "../schema";

/** Upsert a chat member (optimistic tracking from message activity) */
export async function upsertChatMember(
	db: Database,
	chatId: string,
	userId: string,
	role = "member",
): Promise<void> {
	await db
		.insert(chatMembers)
		.values({
			chatId,
			userId,
			role,
			isActive: true,
			lastSeenAt: new Date(),
			cachedAt: new Date(),
		})
		.onConflictDoUpdate({
			target: [chatMembers.chatId, chatMembers.userId],
			set: {
				isActive: true,
				lastSeenAt: new Date(),
			},
		});
}

/** Get active chat members for context building (scoped to specific user IDs) */
export async function getActiveChatMembers(
	db: Database,
	chatId: string,
	userIds: string[],
): Promise<(typeof chatMembers.$inferSelect)[]> {
	if (userIds.length === 0) return [];

	return db
		.select()
		.from(chatMembers)
		.where(
			and(eq(chatMembers.chatId, chatId), inArray(chatMembers.userId, userIds)),
		);
}

/** Get all active chat members */
export async function getAllChatMembers(
	db: Database,
	chatId: string,
): Promise<(typeof chatMembers.$inferSelect)[]> {
	return db
		.select()
		.from(chatMembers)
		.where(and(eq(chatMembers.chatId, chatId), eq(chatMembers.isActive, true)));
}

/** Update member role */
export async function updateMemberRole(
	db: Database,
	chatId: string,
	userId: string,
	role: string,
	isActive: boolean,
): Promise<void> {
	await db
		.update(chatMembers)
		.set({ role, isActive, cachedAt: new Date() })
		.where(and(eq(chatMembers.chatId, chatId), eq(chatMembers.userId, userId)));
}

/** Get chat members joined with user data — for context building */
export async function getMembersWithUsers(
	db: Database,
	chatId: string,
	userIds: string[],
): Promise<
	Array<{
		userId: string;
		telegramId: number;
		firstName: string;
		lastName: string | null;
		username: string | null;
		role: string;
	}>
> {
	if (userIds.length === 0) return [];

	return db
		.select({
			userId: chatMembers.userId,
			telegramId: users.telegramId,
			firstName: users.firstName,
			lastName: users.lastName,
			username: users.username,
			role: chatMembers.role,
		})
		.from(chatMembers)
		.innerJoin(users, eq(chatMembers.userId, users.id))
		.where(
			and(eq(chatMembers.chatId, chatId), inArray(chatMembers.userId, userIds)),
		);
}
