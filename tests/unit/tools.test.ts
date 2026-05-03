import { describe, expect, test } from "bun:test";
import type { CreditService } from "../../src/credits/service";
import type { Database } from "../../src/db/connection";
import type { Chat, User } from "../../src/db/schema";
import type { ContextParticipant } from "../../src/llm/context-builder";
import { ModelTier } from "../../src/llm/registry";
import type { MediaAttachment } from "../../src/llm/types";
import { getMemberTool } from "../../src/tools/get-member";
import { memoryTool } from "../../src/tools/memory";
import { remindTool } from "../../src/tools/remind";
import type { ToolContext } from "../../src/tools/types";

function makeToolContext(overrides: Partial<ToolContext> = {}): ToolContext {
	return {
		db: {} as Database,
		user: { id: "user-1", telegramId: 111, firstName: "Alice" } as User,
		chat: {
			id: "chat-1",
			telegramId: 222,
			memory: "existing memory",
			settings: { memoryAccess: "admins", remindersAccess: "admins" },
		} as Chat,
		creditService: {} as CreditService,
		tier: ModelTier.FREE,
		isChatAdmin: false,
		canManageMemory: false,
		canManageReminders: false,
		sendMessage: async () => {},
		sendPhoto: async () => {},
		sendVoice: async () => {},
		sendVideo: async () => {},
		editMessage: async () => {},
		deleteMessage: async () => {},
		...overrides,
	};
}

describe("getMemberTool", () => {
	test("does not accept raw Telegram user IDs", () => {
		expect(getMemberTool.parameters.safeParse({ userId: 12345 }).success).toBe(
			false,
		);
	});

	test("loads a scoped participant photo into replyMedia", async () => {
		const participant: ContextParticipant = {
			userId: "user-1",
			telegramId: 12345,
			firstName: "Alice",
			lastName: null,
			username: "alice",
			role: "member",
		};
		const media: MediaAttachment = {
			type: "image",
			data: Buffer.from("photo"),
			mimeType: "image/jpeg",
			fileId: "file-1",
		};
		const ctx = makeToolContext({
			participants: new Map([["p1", participant]]),
			getParticipantProfilePhoto: async () => media,
		});

		const result = await getMemberTool.execute({ participantRef: "p1" }, ctx);

		expect(result.error).toBeUndefined();
		expect(result.text).toContain("Loaded Alice");
		expect(ctx.replyMedia?.at(0)).toBe(media);
	});
});

describe("memoryTool", () => {
	test("blocks memory updates without permission", async () => {
		const result = await memoryTool.execute(
			{ action: "update", content: "new fact" },
			makeToolContext({ canManageMemory: false }),
		);

		expect(result.error).toBe("Unauthorized");
	});
});

describe("remindTool command parsing", () => {
	test("parses list and cancel subcommands", () => {
		expect(remindTool.parseCommand?.("list")).toEqual({ action: "list" });
		expect(remindTool.parseCommand?.("cancel abc123")).toEqual({
			action: "cancel",
			reminderId: "abc123",
		});
	});

	test("parses one-time and recurring create commands", () => {
		expect(
			remindTool.parseCommand?.("at 2026-01-01T10:00:00Z standup"),
		).toEqual({
			action: "create",
			fireAt: "2026-01-01T10:00:00Z",
			description: "standup",
			message: "standup",
		});
		expect(remindTool.parseCommand?.("cron 0 9 * * 1-5 | standup")).toEqual({
			action: "create",
			cronExpression: "0 9 * * 1-5",
			description: "standup",
			message: "standup",
		});
	});
});
