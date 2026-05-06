import { describe, expect, test } from "bun:test";
import type { Chat, User } from "../../src/db/schema";
import { ModelTier } from "../../src/llm/registry";
import { remindTool } from "../../src/tools/remind";
import type { ToolContext } from "../../src/tools/types";

function makeToolContext(
    capturedValues: Record<string, unknown>[],
    overrides: Partial<ToolContext> = {},
): ToolContext {
    return {
        user: { id: "user-1", telegramId: 111, firstName: "Alice" } as User,
        chat: {
            id: "chat-1",
            telegramId: 222,
            settings: { memoryAccess: "admins", remindersAccess: "admins" },
        } as Chat,
        providerRecorder: {
            start: async () => null,
            finish: async () => {},
            fail: async () => {},
        },
        memoryStore: { update: async () => {} },
        reminderStore: {
            countActive: async () => 0,
            countRecurringForUser: async () => 0,
            create: async (values) => {
                capturedValues.push({
                    chatId: "chat-1",
                    userId: "user-1",
                    ...values,
                });
            },
            list: async () => [],
            getById: async () => null,
            cancel: async () => {},
        },
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

        const result = await remindTool.execute(
            {
                action: "create",
                description: "daily standup",
                message: "daily standup",
                cronExpression: "0 9 * * *",
            },
            makeToolContext(capturedValues),
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

        const result = await remindTool.execute(
            {
                action: "create",
                description: "thread reminder",
                message: "thread reminder",
                cronExpression: "0 9 * * *",
            },
            makeToolContext(capturedValues, {
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

        const result = await remindTool.execute(
            {
                action: "create",
                description: "daily generated briefing",
                prompt: "Generate a briefing",
                cronExpression: "0 9 * * *",
            },
            makeToolContext(capturedValues),
        );

        expect(result.error).toBe("Recurring LLM reminders disabled");
        expect(capturedValues).toHaveLength(0);
    });
});
