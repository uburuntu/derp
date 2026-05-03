import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { and, eq, sql } from "drizzle-orm";
import type { Chat as TelegramChat, User as TelegramUser } from "grammy/types";
import { closeDb, type Database, getDb } from "../../src/db/connection";
import { upsertChat } from "../../src/db/queries/chats";
import {
	addUserCreditsWithResult,
	applySubscriptionPayment,
	applyUserPackPayment,
	deductUserCredits,
	getDailyUsage,
	reconcileStarRefund,
	recordFreeToolUsage,
} from "../../src/db/queries/credits";
import { upsertUser } from "../../src/db/queries/users";
import { ledger, paymentReceipts, users } from "../../src/db/schema";

const databaseUrl =
	process.env.DERP_RUN_DB_TESTS === "1" ? process.env.DATABASE_URL : undefined;
let db: Database;
let sequence = 0;

describe.skipIf(!databaseUrl)("credits database invariants", () => {
	beforeAll(() => {
		db = getDb(databaseUrl as string);
	});

	afterAll(async () => {
		await closeDb();
	});

	test("applies a Stars pack charge exactly once", async () => {
		const { user } = await createActor("pack");
		const chargeId = uniqueKey("pack-charge");
		const record = {
			userId: user.id,
			chatId: null,
			telegramChargeId: chargeId,
			providerChargeId: null,
			invoicePayload: "pack:micro:user",
			currency: "XTR",
			stars: 50,
			productType: "pack" as const,
			productId: "micro",
			creditTarget: "user" as const,
			credits: 50,
			meta: { test: true },
		};

		const first = await applyUserPackPayment(db, record);
		const duplicate = await applyUserPackPayment(db, record);

		expect(first.applied).toBe(true);
		expect(first.balanceAfter).toBe(50);
		expect(duplicate.applied).toBe(false);
		expect(duplicate.balanceAfter).toBe(50);

		const [projection] = await db
			.select({ credits: users.credits })
			.from(users)
			.where(eq(users.id, user.id))
			.limit(1);
		expect(projection?.credits).toBe(50);
		expect(await countPaymentReceipts(chargeId)).toBe(1);
		expect(await countPositivePaymentLedger(chargeId)).toBe(1);
	});

	test("keeps the winning subscription projection and recomputes after refund", async () => {
		const { user } = await createActor("sub");
		const laterCharge = uniqueKey("sub-later");
		const earlierCharge = uniqueKey("sub-earlier");
		const laterExpiry = new Date(Date.now() + 60 * 24 * 60 * 60 * 1000);
		const earlierExpiry = new Date(Date.now() + 30 * 24 * 60 * 60 * 1000);

		await applySubscriptionPayment(
			db,
			user.id,
			2500,
			"ultra",
			laterCharge,
			laterExpiry,
			{
				chatId: null,
				providerChargeId: null,
				invoicePayload: "sub:ultra",
				currency: "XTR",
				stars: 1500,
			},
			{ test: true },
		);
		await applySubscriptionPayment(
			db,
			user.id,
			200,
			"lite",
			earlierCharge,
			earlierExpiry,
			{
				chatId: null,
				providerChargeId: null,
				invoicePayload: "sub:lite",
				currency: "XTR",
				stars: 150,
			},
			{ test: true },
		);

		let projection = await getUserProjection(user.id);
		expect(projection?.credits).toBe(2700);
		expect(projection?.subscriptionTier).toBe("ultra");
		expect(projection?.subscriptionExpiresAt?.getTime()).toBeGreaterThan(
			earlierExpiry.getTime(),
		);

		const refund = await reconcileStarRefund(db, laterCharge, { test: true });
		expect(refund.applied).toBe(true);
		expect(refund.recoveredAmount).toBe(2500);
		expect(refund.unrecoveredAmount).toBe(0);

		projection = await getUserProjection(user.id);
		expect(projection?.credits).toBe(200);
		expect(projection?.subscriptionTier).toBe("lite");
		expect(projection?.subscriptionExpiresAt?.getTime()).toBeLessThan(
			laterExpiry.getTime(),
		);
	});

	test("reserves free quota atomically under concurrent calls", async () => {
		const { user, chat } = await createActor("quota");
		const toolName = uniqueKey("quota-tool");

		const results = await Promise.all(
			Array.from({ length: 5 }, () =>
				recordFreeToolUsage(db, user.id, chat.id, toolName, null, 3),
			),
		);

		expect(results.filter((result) => result === "applied")).toHaveLength(3);
		expect(
			results.filter((result) => result === "quota_exhausted"),
		).toHaveLength(2);
		expect(await getDailyUsage(db, user.id, chat.id, toolName)).toBe(3);
	});

	test("keeps paid debit and refund idempotent", async () => {
		const { user } = await createActor("debit");
		const spendKey = uniqueKey("tool-spend");
		await addUserCreditsWithResult(
			db,
			user.id,
			10,
			"grant",
			undefined,
			uniqueKey("seed"),
		);

		const afterDebit = await deductUserCredits(
			db,
			user.id,
			4,
			"imagine",
			"gemini-test",
			spendKey,
		);
		const duplicateDebit = await deductUserCredits(
			db,
			user.id,
			4,
			"imagine",
			"gemini-test",
			spendKey,
		);
		const refund = await addUserCreditsWithResult(
			db,
			user.id,
			4,
			"refund",
			undefined,
			`${spendKey}:refund`,
			{ reason: "test_refund" },
		);
		const duplicateRefund = await addUserCreditsWithResult(
			db,
			user.id,
			4,
			"refund",
			undefined,
			`${spendKey}:refund`,
			{ reason: "test_refund" },
		);

		expect(afterDebit).toBe(6);
		expect(duplicateDebit).toBe(6);
		expect(refund.applied).toBe(true);
		expect(refund.balanceAfter).toBe(10);
		expect(duplicateRefund.applied).toBe(false);
		expect(duplicateRefund.balanceAfter).toBe(10);
	});
});

async function createActor(label: string) {
	const id = nextTelegramId();
	const user = await upsertUser(db, {
		id,
		is_bot: false,
		first_name: `Test ${label}`,
		username: `derp_${label}_${sequence}`,
	} as TelegramUser);
	const chat = await upsertChat(db, {
		id: id + 1,
		type: "private",
		first_name: `Test ${label}`,
		username: `derp_chat_${label}_${sequence}`,
	} as TelegramChat);

	return { user, chat };
}

async function getUserProjection(userId: string) {
	const [row] = await db
		.select({
			credits: users.credits,
			subscriptionTier: users.subscriptionTier,
			subscriptionExpiresAt: users.subscriptionExpiresAt,
		})
		.from(users)
		.where(eq(users.id, userId))
		.limit(1);
	return row ?? null;
}

async function countPaymentReceipts(chargeId: string): Promise<number> {
	const [row] = await db
		.select({ count: sql<number>`count(*)::int` })
		.from(paymentReceipts)
		.where(eq(paymentReceipts.telegramChargeId, chargeId));
	return row?.count ?? 0;
}

async function countPositivePaymentLedger(chargeId: string): Promise<number> {
	const [row] = await db
		.select({ count: sql<number>`count(*)::int` })
		.from(ledger)
		.where(
			and(
				eq(ledger.telegramChargeId, chargeId),
				sql`${ledger.amount} > 0`,
				sql`${ledger.type} IN ('purchase', 'subscription')`,
			),
		);
	return row?.count ?? 0;
}

function uniqueKey(prefix: string): string {
	sequence += 1;
	return `${prefix}-${Date.now()}-${process.pid}-${sequence}`;
}

function nextTelegramId(): number {
	sequence += 1;
	return 7_000_000_000 + (Date.now() % 1_000_000) * 100 + sequence;
}
