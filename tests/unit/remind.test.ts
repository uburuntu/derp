import { describe, expect, test } from "bun:test";
import type { CreditService } from "../../src/credits/service";
import type { Database } from "../../src/db/connection";
import type { Chat, User } from "../../src/db/schema";
import { ModelTier } from "../../src/llm/registry";
import { remindTool } from "../../src/tools/remind";
import type { ToolContext } from "../../src/tools/types";

function makeReminderDb(capturedValues: Record<string, unknown>[]): Database {
	return {
		select: () => ({
			from: () => ({
				where: async () => [{ count: 0 }],
			}),
		}),
		insert: () => ({
			values: (values: Record<string, unknown>) => ({
				returning: async () => {
					capturedValues.push(values);
					return [
						{
							id: "reminder-1",
							...values,
							status: "active",
							lastFiredAt: null,
							fireCount: 0,
							meta: null,
							createdAt: new Date(),
							updatedAt: new Date(),
						},
					];
				},
			}),
		}),
	} as unknown as Database;
}

function makeToolContext(
	db: Database,
	overrides: Partial<ToolContext> = {},
): ToolContext {
	return {
		db,
		user: { id: "user-1", telegramId: 111, firstName: "Alice" } as User,
		chat: {
			id: "chat-1",
			telegramId: 222,
			settings: { memoryAccess: "admins", remindersAccess: "admins" },
		} as Chat,
		creditService: {} as CreditService,
		tier: ModelTier.FREE,
		isChatAdmin: true,
		canManageMemory: true,
		canManageReminders: true,
		sendMessage: async () => {},
		sendPhoto: async () => {},
		sendVoice: async () => {},
		sendVideo: async () => {},
		editMessage: async () => {},
		deleteMessage: async () => {},
		...overrides,
	};
}

describe("remindTool create", () => {
	test("stores initial next fire time for recurring reminders", async () => {
		const capturedValues: Record<string, unknown>[] = [];
		const db = makeReminderDb(capturedValues);

		const result = await remindTool.execute(
			{
				action: "create",
				description: "daily standup",
				message: "daily standup",
				cronExpression: "0 9 * * *",
			},
			makeToolContext(db),
		);

		expect(result.error).toBeUndefined();
		const created = capturedValues[0];
		expect(created?.isRecurring).toBe(true);
		expect(created?.cronExpression).toBe("0 9 * * *");
		expect(created?.fireAt).toBeInstanceOf(Date);
		expect((created?.fireAt as Date).getTime()).toBeGreaterThan(Date.now());
	});

	test("preserves thread and reply metadata when creating reminders", async () => {
		const capturedValues: Record<string, unknown>[] = [];
		const db = makeReminderDb(capturedValues);

		const result = await remindTool.execute(
			{
				action: "create",
				description: "thread reminder",
				message: "thread reminder",
				cronExpression: "0 9 * * *",
			},
			makeToolContext(db, {
				threadId: 321,
				replyToMessageId: 654,
			}),
		);

		expect(result.error).toBeUndefined();
		const created = capturedValues[0];
		expect(created?.threadId).toBe(321);
		expect(created?.replyToMessageId).toBe(654);
	});

	test("rejects recurring LLM reminders", async () => {
		const capturedValues: Record<string, unknown>[] = [];
		const db = makeReminderDb(capturedValues);

		const result = await remindTool.execute(
			{
				action: "create",
				description: "daily generated briefing",
				prompt: "Generate a briefing",
				cronExpression: "0 9 * * *",
			},
			makeToolContext(db),
		);

		expect(result.error).toBe("Recurring LLM reminders disabled");
		expect(capturedValues).toHaveLength(0);
	});
});
