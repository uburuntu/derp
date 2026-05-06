import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { and, desc, eq } from "drizzle-orm";
import { z } from "zod";
import type { Database } from "../../src/db/connection";
import type { ToolDefinition } from "../../src/tools/types";
import {
    configuredAdminId,
    createE2EHarness,
    type E2EHarness,
    ensureE2EEnv,
    shouldRunE2E,
} from "./harness";
import {
    callbackUpdate,
    messageUpdate,
    preCheckoutUpdate,
    successfulPaymentUpdate,
    testActor,
    testGroup,
} from "./telegram-updates";

let closeDb: (() => Promise<void>) | undefined;

describe.skipIf(!shouldRunE2E())("bot e2e", () => {
    beforeAll(async () => {
        ensureE2EEnv();
        ({ closeDb } = await import("../../src/db/connection"));
    });

    afterAll(async () => {
        await closeDb?.();
    });

    test("handles private onboarding and persists the incoming command", async () => {
        const harness = await createE2EHarness();
        const actor = testActor("start");
        const messageId = 3101;
        const update = messageUpdate({ actor, text: "/start", messageId });

        await harness.handle(update);

        const reply = harness.api.lastCall("sendMessage");
        expect(reply?.payload.text).toContain("Derp is ready.");
        expect(reply?.payload.parse_mode).toBe("HTML");

        const { users, chats, messages } = await import("../../src/db/schema");
        const [user] = await harness.db
            .select()
            .from(users)
            .where(eq(users.telegramId, actor.user.id))
            .limit(1);
        const [chat] = await harness.db
            .select()
            .from(chats)
            .where(eq(chats.telegramId, actor.privateChat.id))
            .limit(1);
        const [message] = await harness.db
            .select()
            .from(messages)
            .where(
                and(
                    eq(messages.chatId, chat?.id ?? ""),
                    eq(messages.telegramMessageId, messageId),
                ),
            )
            .limit(1);

        expect(user?.telegramId).toBe(actor.user.id);
        expect(chat?.telegramId).toBe(actor.privateChat.id);
        expect(message?.text).toBe("/start");
        expect(message?.direction).toBe("in");
    });

    test("renders help in a group topic with reply threading", async () => {
        const helpTool = testTool({
            name: "e2eHelpTool",
            commands: ["/e2e_help"],
            credits: 0,
            freeDaily: 0,
        });
        const harness = await createE2EHarness({ tools: [helpTool] });
        const actor = testActor("help");
        const group = testGroup("help");

        await harness.handle(
            messageUpdate({
                actor,
                chat: group,
                text: "/help",
                messageId: 4101,
                threadId: 77,
            }),
        );

        const reply = harness.api.lastCall("sendMessage");
        expect(reply?.payload.text).toContain("🤖 <b>Derp</b>");
        expect(reply?.payload.text).toContain("/e2e_help");
        expect(reply?.payload.message_thread_id).toBe(77);
        expect(reply?.payload.reply_to_message_id).toBe(4101);
    });

    test("creates a personal pack invoice from the buy flow", async () => {
        const harness = await createE2EHarness();
        const actor = testActor("buy");

        await harness.handle(messageUpdate({ actor, text: "/buy" }));
        const buyReply = harness.api.lastCall("sendMessage");
        expect(buyReply?.payload.text).toContain("Add credits");
        expect(buyReply?.payload.reply_markup).toBeTruthy();

        await harness.handle(
            callbackUpdate({
                actor,
                data: "pack:micro",
                messageId: Number(
                    (buyReply?.result as { message_id?: number } | undefined)
                        ?.message_id ?? 5001,
                ),
            }),
        );

        const invoice = harness.api.lastCall("sendInvoice");
        expect(invoice?.payload.chat_id).toBe(actor.privateChat.id);
        expect(invoice?.payload.currency).toBe("XTR");
        expect(invoice?.payload.payload).toEqual(
            expect.stringContaining("pay:p:micro:u:"),
        );
        expect(invoice?.payload.prices).toEqual([
            expect.objectContaining({ amount: 50 }),
        ]);
    });

    test("settles a successful personal pack payment", async () => {
        const harness = await createE2EHarness();
        const actor = testActor("payment");
        const payload = await createMicroPackInvoicePayload(harness, actor);

        await harness.handle(
            preCheckoutUpdate({
                actor,
                payload,
                totalAmount: 50,
            }),
        );
        expect(harness.api.lastCall("answerPreCheckoutQuery")?.payload.ok).toBe(
            true,
        );

        const chargeId = uniqueCharge("pack");
        await harness.handle(
            successfulPaymentUpdate({
                actor,
                payload,
                totalAmount: 50,
                chargeId,
            }),
        );

        const { users, paymentReceipts } = await import("../../src/db/schema");
        const [user] = await harness.db
            .select()
            .from(users)
            .where(eq(users.telegramId, actor.user.id))
            .limit(1);
        const [receipt] = await harness.db
            .select()
            .from(paymentReceipts)
            .where(eq(paymentReceipts.telegramChargeId, chargeId))
            .limit(1);
        const acknowledgement = harness.api
            .callsFor("sendMessage")
            .find((call) =>
                String(call.payload.text ?? "").includes("Credits added"),
            );

        expect(user?.credits).toBe(50);
        expect(receipt?.status).toBe("settled");
        expect(receipt?.credits).toBe(50);
        expect(acknowledgement?.payload.chat_id).toBe(actor.privateChat.id);
    });

    test("rejects invalid pre-checkout payments", async () => {
        const harness = await createE2EHarness();
        const actor = testActor("invalid-payment");
        const payload = await createMicroPackInvoicePayload(harness, actor);

        await harness.handle(
            preCheckoutUpdate({
                actor,
                payload,
                currency: "USD",
                totalAmount: 50,
            }),
        );

        const answer = harness.api.lastCall("answerPreCheckoutQuery");
        expect(answer?.payload.ok).toBe(false);
        expect(answer?.payload.error_message).toContain("Payment rejected");
    });

    test("confirms and charges an expensive tool once", async () => {
        const tool = testTool({
            name: "e2eConfirm",
            commands: ["/e2e_confirm"],
            credits: 20,
            freeDaily: 0,
        });
        const harness = await createE2EHarness({ tools: [tool] });
        const actor = testActor("confirm");
        await seedUserCredits(harness.db, actor, 30);

        await harness.handle(
            messageUpdate({
                actor,
                text: "/e2e_confirm ship it",
                messageId: 6101,
            }),
        );

        const confirmation = harness.api.lastCall("sendMessage");
        expect(confirmation?.payload.text).toContain("Confirm spend");
        const callbackData = firstCallbackData(confirmation?.payload);
        expect(callbackData).toContain("tool_confirm:run:");

        await harness.handle(
            callbackUpdate({
                actor,
                data: callbackData,
                messageId: Number(
                    (
                        confirmation?.result as
                            | { message_id?: number }
                            | undefined
                    )?.message_id ?? 6102,
                ),
                messageText: String(confirmation?.payload.text ?? ""),
            }),
        );

        const resultReply = harness.api
            .callsFor("sendMessage")
            .find((call) =>
                String(call.payload.text ?? "").includes("tool ran: ship it"),
            );
        const doneEdit = harness.api
            .callsFor("editMessageText")
            .find((call) => String(call.payload.text ?? "").includes("Done"));
        const { users, ledger } = await import("../../src/db/schema");
        const [user] = await harness.db
            .select()
            .from(users)
            .where(eq(users.telegramId, actor.user.id))
            .limit(1);
        const [spend] = await harness.db
            .select()
            .from(ledger)
            .where(
                and(
                    eq(ledger.userId, user?.id ?? ""),
                    eq(ledger.toolName, tool.name),
                ),
            )
            .orderBy(desc(ledger.createdAt))
            .limit(1);

        expect(resultReply).toBeTruthy();
        expect(doneEdit).toBeTruthy();
        expect(user?.credits).toBe(10);
        expect(spend?.amount).toBe(-20);
        expect(spend?.meta?.spendStatus).toBe("delivered");
    });

    test("serves basic admin diagnostics and metrics", async () => {
        const adminId = configuredAdminId();
        const harness = await createE2EHarness();
        const actor = testActor("admin", adminId);

        await harness.handle(messageUpdate({ actor, text: "/admin status" }));
        expect(harness.api.lastCall("sendMessage")?.payload.text).toContain(
            "Bot Status",
        );

        await harness.handle(
            messageUpdate({ actor, text: "/admin metrics 1" }),
        );
        expect(harness.api.lastCall("sendMessage")?.payload.text).toContain(
            "Usage Metrics",
        );
    });
});

async function createMicroPackInvoicePayload(
    harness: E2EHarness,
    actor: ReturnType<typeof testActor>,
): Promise<string> {
    await harness.handle(messageUpdate({ actor, text: "/buy" }));
    await harness.handle(callbackUpdate({ actor, data: "pack:micro" }));
    const invoice = harness.api.lastCall("sendInvoice");
    const payload = invoice?.payload.payload;
    if (typeof payload !== "string") {
        throw new Error("sendInvoice did not include a string payload");
    }
    return payload;
}

async function seedUserCredits(
    db: Database,
    actor: ReturnType<typeof testActor>,
    amount: number,
): Promise<void> {
    const [{ upsertUser }, { upsertChat }, { addUserCreditsWithResult }] =
        await Promise.all([
            import("../../src/db/queries/users"),
            import("../../src/db/queries/chats"),
            import("../../src/db/queries/credits"),
        ]);
    const user = await upsertUser(db, actor.user);
    await upsertChat(db, actor.privateChat);
    await addUserCreditsWithResult(
        db,
        user.id,
        amount,
        "grant",
        undefined,
        `e2e-seed:${actor.user.id}:${Date.now()}`,
        { source: "e2e" },
    );
}

function testTool(input: {
    name: string;
    commands: string[];
    credits: number;
    freeDaily: number;
}): ToolDefinition {
    const parameters = z.object({ text: z.string() });
    return {
        name: input.name,
        commands: input.commands,
        description: "E2E deterministic tool",
        helpText: "cmd-info-desc",
        category: "utility",
        parameters,
        parseCommand: (text) => ({ text: text.trim() || "ok" }),
        execute: async (params) => {
            const parsed = parameters.parse(params);
            return { text: `tool ran: ${parsed.text}` };
        },
        credits: input.credits,
        freeDaily: input.freeDaily,
    };
}

function firstCallbackData(payload: unknown): string {
    const markup = (payload as { reply_markup?: unknown } | undefined)
        ?.reply_markup;
    const keyboard = (
        markup as
            | {
                  inline_keyboard?: Array<Array<{ callback_data?: string }>>;
              }
            | undefined
    )?.inline_keyboard;
    const callbackData = keyboard?.[0]?.[0]?.callback_data;
    if (!callbackData) throw new Error("No callback_data in inline keyboard");
    return callbackData;
}

function uniqueCharge(prefix: string): string {
    return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
