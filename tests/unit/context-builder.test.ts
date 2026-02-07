import { describe, expect, test } from "bun:test";
import type { Message } from "../../src/db/schema";
import {
	buildContext,
	type ContextParticipant,
} from "../../src/llm/context-builder";

function makeMessage(overrides: Partial<Message> = {}): Message {
	return {
		id: "msg-1",
		chatId: "chat-1",
		userId: "user-1",
		telegramMessageId: 100,
		threadId: null,
		direction: "in",
		contentType: "text",
		text: "Hello world",
		mediaGroupId: null,
		attachmentType: null,
		attachmentFileId: null,
		replyToMessageId: null,
		metadata: null,
		telegramDate: new Date(),
		editedAt: null,
		deletedAt: null,
		createdAt: new Date(),
		updatedAt: new Date(),
		...overrides,
	};
}

describe("buildContext", () => {
	test("builds participant registry with active users", () => {
		const members = new Map<string, ContextParticipant>();
		members.set("user-1", {
			userId: "user-1",
			telegramId: 12345,
			firstName: "Alice",
			lastName: null,
			username: "alice",
			role: "member",
		});

		const msgs = [makeMessage()];
		const result = buildContext(msgs, members, "DerpRobot");

		expect(result.participants).toContain("Alice");
		expect(result.participants).toContain("@alice");
		expect(result.participants).toContain("Derp");
		expect(result.participants).toContain("@DerpRobot");
	});

	test("builds message stream with sender names", () => {
		const members = new Map<string, ContextParticipant>();
		members.set("user-1", {
			userId: "user-1",
			telegramId: 12345,
			firstName: "Alice",
			lastName: null,
			username: null,
			role: "member",
		});

		const msgs = [makeMessage({ text: "Hi there" })];
		const result = buildContext(msgs, members, "DerpRobot");

		expect(result.messageStream).toContain("[100] Alice: Hi there");
	});

	test("uses 'Derp' for outgoing messages", () => {
		const members = new Map<string, ContextParticipant>();
		const msgs = [
			makeMessage({ direction: "out", userId: null, text: "Hello!" }),
		];
		const result = buildContext(msgs, members, "DerpRobot");

		expect(result.messageStream).toContain("Derp: Hello!");
	});

	test("includes reply references", () => {
		const members = new Map<string, ContextParticipant>();
		members.set("user-1", {
			userId: "user-1",
			telegramId: 12345,
			firstName: "Alice",
			lastName: null,
			username: null,
			role: "member",
		});

		const msgs = [makeMessage({ replyToMessageId: 50, text: "Replying" })];
		const result = buildContext(msgs, members, "DerpRobot");

		expect(result.messageStream).toContain("(→50)");
	});

	test("includes media tags", () => {
		const members = new Map<string, ContextParticipant>();
		members.set("user-1", {
			userId: "user-1",
			telegramId: 12345,
			firstName: "Alice",
			lastName: null,
			username: null,
			role: "member",
		});

		const msgs = [
			makeMessage({
				attachmentType: "photo",
				attachmentFileId: "abc123",
				text: "Look at this",
			}),
		];
		const result = buildContext(msgs, members, "DerpRobot");

		expect(result.messageStream).toContain("[photo file_id:abc123]");
		expect(result.messageStream).toContain("Look at this");
	});

	test("includes role tags for non-members", () => {
		const members = new Map<string, ContextParticipant>();
		members.set("user-1", {
			userId: "user-1",
			telegramId: 12345,
			firstName: "Admin",
			lastName: null,
			username: null,
			role: "administrator",
		});

		const msgs = [makeMessage()];
		const result = buildContext(msgs, members, "DerpRobot");

		expect(result.participants).toContain("[administrator]");
	});

	test("only includes participants who appear in messages", () => {
		const members = new Map<string, ContextParticipant>();
		members.set("user-1", {
			userId: "user-1",
			telegramId: 12345,
			firstName: "Active",
			lastName: null,
			username: null,
			role: "member",
		});
		members.set("user-2", {
			userId: "user-2",
			telegramId: 67890,
			firstName: "Inactive",
			lastName: null,
			username: null,
			role: "member",
		});

		const msgs = [makeMessage({ userId: "user-1" })];
		const result = buildContext(msgs, members, "DerpRobot");

		expect(result.participants).toContain("Active");
		expect(result.participants).not.toContain("Inactive");
	});
});
