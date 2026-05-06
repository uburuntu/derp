import { and, desc, eq, sql } from "drizzle-orm";
import type { Database } from "../connection";
import {
    chats,
    ledger,
    paymentReceipts,
    subscriptionPeriods,
    users,
} from "../schema";
import type { IdempotentCreditResult } from "./credit-ledger";
import { createCreditDebt, settleOpenDebts } from "./finance";

type CreditTransaction = Parameters<Parameters<Database["transaction"]>[0]>[0];

export type { IdempotentCreditResult } from "./credit-ledger";
export {
    addChatCredits,
    addChatCreditsWithResult,
    addUserCredits,
    addUserCreditsWithResult,
    deductChatCredits,
    deductUserCredits,
    getBalances,
    getTransactionByIdempotencyKey,
    markLedgerSpendStatus,
} from "./credit-ledger";
export type { ToolDebitResult } from "./credit-usage";
export {
    getDailyUsage,
    incrementDailyUsage,
    recordFreeToolUsage,
} from "./credit-usage";

export interface RefundReconciliationResult {
    applied: boolean;
    target: "user" | "chat";
    originalType: string;
    originalAmount: number;
    recoveredAmount: number;
    unrecoveredAmount: number;
    balanceAfter: number;
}

export interface StarsPaymentRecord {
    userId: string;
    chatId?: string | null;
    telegramChargeId: string;
    providerChargeId?: string | null;
    invoicePayload?: string | null;
    currency: string;
    stars: number;
    productType: "subscription" | "pack" | "donation" | "unknown";
    productId?: string | null;
    creditTarget: "user" | "chat" | "none";
    credits: number;
    meta?: Record<string, unknown>;
}

async function recordPaymentReceiptIn(
    tx: CreditTransaction,
    record: StarsPaymentRecord,
): Promise<{ id: string; needsSettlement: boolean }> {
    const [inserted] = await tx
        .insert(paymentReceipts)
        .values({
            userId: record.userId,
            chatId: record.chatId ?? null,
            telegramChargeId: record.telegramChargeId,
            providerChargeId: record.providerChargeId ?? null,
            invoicePayload: record.invoicePayload ?? null,
            currency: record.currency,
            stars: record.stars,
            productType: record.productType,
            productId: record.productId ?? null,
            creditTarget: record.creditTarget,
            credits: record.credits,
            status: "received",
            meta: record.meta,
        })
        .onConflictDoNothing({ target: paymentReceipts.telegramChargeId })
        .returning({ id: paymentReceipts.id });

    if (inserted) return { id: inserted.id, needsSettlement: true };

    const [existing] = await tx
        .select({ id: paymentReceipts.id, status: paymentReceipts.status })
        .from(paymentReceipts)
        .where(eq(paymentReceipts.telegramChargeId, record.telegramChargeId))
        .for("update")
        .limit(1);

    if (!existing) throw new Error("Payment receipt conflict without row");
    return {
        id: existing.id,
        needsSettlement:
            existing.status === "received" ||
            existing.status === "settlement_failed",
    };
}

async function ensurePaymentReceiptReceived(
    db: Database,
    record: StarsPaymentRecord,
): Promise<void> {
    await db
        .insert(paymentReceipts)
        .values({
            userId: record.userId,
            chatId: record.chatId ?? null,
            telegramChargeId: record.telegramChargeId,
            providerChargeId: record.providerChargeId ?? null,
            invoicePayload: record.invoicePayload ?? null,
            currency: record.currency,
            stars: record.stars,
            productType: record.productType,
            productId: record.productId ?? null,
            creditTarget: record.creditTarget,
            credits: record.credits,
            status: "received",
            meta: record.meta,
        })
        .onConflictDoNothing({ target: paymentReceipts.telegramChargeId });
}

export async function recordFailedPaymentReceipt(
    db: Database,
    record: StarsPaymentRecord,
    error: string,
): Promise<void> {
    const [inserted] = await db
        .insert(paymentReceipts)
        .values({
            userId: record.userId,
            chatId: record.chatId ?? null,
            telegramChargeId: record.telegramChargeId,
            providerChargeId: record.providerChargeId ?? null,
            invoicePayload: record.invoicePayload ?? null,
            currency: record.currency,
            stars: record.stars,
            productType: record.productType,
            productId: record.productId ?? null,
            creditTarget: record.creditTarget,
            credits: record.credits,
            status: "settlement_failed",
            meta: {
                ...(record.meta ?? {}),
                lastSettlementError: error,
                lastSettlementFailedAt: new Date().toISOString(),
                settlementAttemptCount: 1,
            },
        })
        .onConflictDoNothing({ target: paymentReceipts.telegramChargeId })
        .returning({ id: paymentReceipts.id });
    if (inserted) return;
    await markPaymentSettlementFailed(db, record.telegramChargeId, error);
}

async function markPaymentSettledIn(
    tx: CreditTransaction,
    receiptId: string,
): Promise<void> {
    await tx
        .update(paymentReceipts)
        .set({ status: "settled", settledAt: new Date() })
        .where(eq(paymentReceipts.id, receiptId));
}

export async function markPaymentSettlementFailed(
    db: Database,
    telegramChargeId: string,
    error?: string,
): Promise<void> {
    await db
        .update(paymentReceipts)
        .set({
            status: "settlement_failed",
            updatedAt: new Date(),
            meta: sql`COALESCE(${paymentReceipts.meta}, '{}'::jsonb) || ${JSON.stringify(
                {
                    lastSettlementError: error ?? null,
                    lastSettlementFailedAt: new Date().toISOString(),
                },
            )}::jsonb || jsonb_build_object(
				'settlementAttemptCount',
				COALESCE((${paymentReceipts.meta}->>'settlementAttemptCount')::int, 0) + 1
			)`,
        })
        .where(
            and(
                eq(paymentReceipts.telegramChargeId, telegramChargeId),
                sql`${paymentReceipts.status} IN ('received', 'settlement_failed')`,
            ),
        );
}

function numberFromMeta(
    meta: Record<string, unknown> | null | undefined,
    key: string,
): number | null {
    const value = meta?.[key];
    return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export async function recordDonationPayment(
    db: Database,
    record: StarsPaymentRecord,
): Promise<IdempotentCreditResult> {
    await ensurePaymentReceiptReceived(db, record);
    return db.transaction(async (tx) => {
        const receipt = await recordPaymentReceiptIn(tx, record);
        const refundKey = `donation:${record.telegramChargeId}`;
        if (!receipt.needsSettlement) {
            const [existing] = await tx
                .select({ balanceAfter: ledger.balanceAfter })
                .from(ledger)
                .where(eq(ledger.idempotencyKey, refundKey))
                .limit(1);
            return {
                balanceAfter: existing?.balanceAfter ?? 0,
                applied: false,
            };
        }

        const [userRow] = await tx
            .select({ credits: users.credits })
            .from(users)
            .where(eq(users.id, record.userId))
            .limit(1);

        await tx.insert(ledger).values({
            userId: record.userId,
            chatId: record.chatId ?? null,
            type: "donation",
            amount: 0,
            balanceAfter: userRow?.credits ?? 0,
            telegramChargeId: record.telegramChargeId,
            idempotencyKey: refundKey,
            meta: { paymentReceiptId: receipt.id },
        });
        await markPaymentSettledIn(tx, receipt.id);

        return { balanceAfter: userRow?.credits ?? 0, applied: true };
    });
}

export async function applyUserPackPayment(
    db: Database,
    record: StarsPaymentRecord,
): Promise<IdempotentCreditResult> {
    await ensurePaymentReceiptReceived(db, record);
    const idempotencyKey = `pack:${record.telegramChargeId}`;

    return db.transaction(async (tx) => {
        const receipt = await recordPaymentReceiptIn(tx, record);
        if (!receipt.needsSettlement) {
            const [existing] = await tx
                .select({ balanceAfter: ledger.balanceAfter })
                .from(ledger)
                .where(eq(ledger.idempotencyKey, idempotencyKey))
                .limit(1);
            return {
                balanceAfter: existing?.balanceAfter ?? 0,
                applied: false,
            };
        }

        const debtSettlement = await settleOpenDebts(tx, {
            userId: record.userId,
            chatId: null,
            amount: record.credits,
            paymentReceiptId: receipt.id,
            meta: { telegramChargeId: record.telegramChargeId },
        });
        const spendableCredits = debtSettlement.remainingAmount;

        const [updated] = await tx
            .update(users)
            .set({ credits: sql`${users.credits} + ${spendableCredits}` })
            .where(eq(users.id, record.userId))
            .returning({ credits: users.credits });

        if (!updated) throw new Error("User not found");

        await tx.insert(ledger).values({
            userId: record.userId,
            type: "purchase",
            amount: spendableCredits,
            balanceAfter: updated.credits,
            telegramChargeId: record.telegramChargeId,
            idempotencyKey,
            meta: {
                paymentReceiptId: receipt.id,
                purchasedCredits: record.credits,
                debtRecovered: debtSettlement.settledAmount,
            },
        });
        await markPaymentSettledIn(tx, receipt.id);

        return {
            balanceAfter: updated.credits,
            applied: true,
            debtRecovered: debtSettlement.settledAmount,
            creditedAmount: spendableCredits,
        };
    });
}

export async function applyChatPackPayment(
    db: Database,
    record: StarsPaymentRecord,
): Promise<IdempotentCreditResult> {
    await ensurePaymentReceiptReceived(db, record);
    if (!record.chatId) throw new Error("Chat payment missing chat ID");
    const chatId = record.chatId;
    const idempotencyKey = `pack:${record.telegramChargeId}`;

    return db.transaction(async (tx) => {
        const receipt = await recordPaymentReceiptIn(tx, record);
        await tx
            .update(paymentReceipts)
            .set({ chatId, updatedAt: new Date() })
            .where(
                and(
                    eq(paymentReceipts.id, receipt.id),
                    sql`${paymentReceipts.chatId} IS NULL`,
                ),
            );
        if (!receipt.needsSettlement) {
            const [existing] = await tx
                .select({ balanceAfter: ledger.balanceAfter })
                .from(ledger)
                .where(eq(ledger.idempotencyKey, idempotencyKey))
                .limit(1);
            return {
                balanceAfter: existing?.balanceAfter ?? 0,
                applied: false,
            };
        }

        const debtSettlement = await settleOpenDebts(tx, {
            userId: record.userId,
            chatId,
            amount: record.credits,
            paymentReceiptId: receipt.id,
            meta: { telegramChargeId: record.telegramChargeId },
        });
        const spendableCredits = debtSettlement.remainingAmount;

        const [updated] = await tx
            .update(chats)
            .set({ credits: sql`${chats.credits} + ${spendableCredits}` })
            .where(eq(chats.id, chatId))
            .returning({ credits: chats.credits });

        if (!updated) throw new Error("Chat not found");

        await tx.insert(ledger).values({
            userId: record.userId,
            chatId,
            type: "purchase",
            amount: spendableCredits,
            balanceAfter: updated.credits,
            telegramChargeId: record.telegramChargeId,
            idempotencyKey,
            meta: {
                paymentReceiptId: receipt.id,
                purchasedCredits: record.credits,
                debtRecovered: debtSettlement.settledAmount,
            },
        });
        await markPaymentSettledIn(tx, receipt.id);

        return {
            balanceAfter: updated.credits,
            applied: true,
            debtRecovered: debtSettlement.settledAmount,
            creditedAmount: spendableCredits,
        };
    });
}

/** Apply a subscription payment and update subscription status atomically. */
export async function applySubscriptionPayment(
    db: Database,
    userId: string,
    amount: number,
    planId: string,
    telegramChargeId: string,
    subscriptionExpiresAt: Date,
    payment: Omit<
        StarsPaymentRecord,
        | "userId"
        | "telegramChargeId"
        | "productType"
        | "productId"
        | "creditTarget"
        | "credits"
    >,
    meta?: Record<string, unknown>,
): Promise<IdempotentCreditResult> {
    const idempotencyKey = `sub:${telegramChargeId}`;
    const paymentMeta = {
        ...meta,
        planId,
        subscriptionExpiresAt: subscriptionExpiresAt.toISOString(),
    };
    await ensurePaymentReceiptReceived(db, {
        ...payment,
        userId,
        telegramChargeId,
        productType: "subscription",
        productId: planId,
        creditTarget: "user",
        credits: amount,
        meta: paymentMeta,
    });

    return db.transaction(async (tx) => {
        const receipt = await recordPaymentReceiptIn(tx, {
            ...payment,
            userId,
            telegramChargeId,
            productType: "subscription",
            productId: planId,
            creditTarget: "user",
            credits: amount,
            meta: paymentMeta,
        });

        if (!receipt.needsSettlement) {
            const [existing] = await tx
                .select({
                    balanceAfter: ledger.balanceAfter,
                    meta: ledger.meta,
                })
                .from(ledger)
                .where(eq(ledger.idempotencyKey, idempotencyKey))
                .limit(1);

            await recomputeActiveSubscriptionIn(tx, userId);

            return {
                balanceAfter: existing?.balanceAfter ?? 0,
                applied: false,
            };
        }

        const debtSettlement = await settleOpenDebts(tx, {
            userId,
            chatId: null,
            amount,
            paymentReceiptId: receipt.id,
            meta: { telegramChargeId },
        });
        const spendableCredits = debtSettlement.remainingAmount;

        const [inserted] = await tx
            .insert(ledger)
            .values({
                userId,
                type: "subscription",
                amount: spendableCredits,
                balanceAfter: 0,
                telegramChargeId,
                idempotencyKey,
                meta: {
                    ...paymentMeta,
                    purchasedCredits: amount,
                    debtRecovered: debtSettlement.settledAmount,
                },
            })
            .onConflictDoNothing({ target: ledger.idempotencyKey })
            .returning({ id: ledger.id });

        if (!inserted) throw new Error("Subscription ledger conflict");

        const [updated] = await tx
            .update(users)
            .set({
                credits: sql`${users.credits} + ${spendableCredits}`,
            })
            .where(eq(users.id, userId))
            .returning({ credits: users.credits });

        if (!updated) throw new Error("User not found");

        await tx
            .update(ledger)
            .set({ balanceAfter: updated.credits })
            .where(eq(ledger.id, inserted.id));

        await tx
            .insert(subscriptionPeriods)
            .values({
                userId,
                paymentId: receipt.id,
                telegramChargeId,
                planId,
                credits: amount,
                expiresAt: subscriptionExpiresAt,
                meta: paymentMeta,
            })
            .onConflictDoNothing({
                target: subscriptionPeriods.telegramChargeId,
            });

        await recomputeActiveSubscriptionIn(tx, userId);
        await markPaymentSettledIn(tx, receipt.id);

        return {
            balanceAfter: updated.credits,
            applied: true,
            debtRecovered: debtSettlement.settledAmount,
            creditedAmount: spendableCredits,
        };
    });
}

function paymentRecordFromReceipt(
    receipt: typeof paymentReceipts.$inferSelect,
): StarsPaymentRecord {
    if (
        receipt.productType !== "subscription" &&
        receipt.productType !== "pack" &&
        receipt.productType !== "donation"
    ) {
        throw new Error(
            `Unsupported payment product type: ${receipt.productType}`,
        );
    }
    if (
        receipt.creditTarget !== "user" &&
        receipt.creditTarget !== "chat" &&
        receipt.creditTarget !== "none"
    ) {
        throw new Error(`Unsupported credit target: ${receipt.creditTarget}`);
    }

    return {
        userId: receipt.userId,
        chatId: receipt.chatId,
        telegramChargeId: receipt.telegramChargeId,
        providerChargeId: receipt.providerChargeId,
        invoicePayload: receipt.invoicePayload,
        currency: receipt.currency,
        stars: receipt.stars,
        productType: receipt.productType,
        productId: receipt.productId,
        creditTarget: receipt.creditTarget,
        credits: receipt.credits,
        meta: receipt.meta ?? undefined,
    };
}

function subscriptionExpiryFromReceipt(
    receipt: typeof paymentReceipts.$inferSelect,
): Date {
    const raw = receipt.meta?.subscriptionExpiresAt;
    if (typeof raw !== "string") {
        throw new Error("Subscription receipt missing expiration metadata");
    }
    const expiresAt = new Date(raw);
    if (Number.isNaN(expiresAt.getTime())) {
        throw new Error("Subscription receipt has invalid expiration metadata");
    }
    return expiresAt;
}

function targetTelegramChatIdFromMeta(
    meta: Record<string, unknown> | null | undefined,
): number | null {
    const raw = meta?.targetTelegramChatId;
    return typeof raw === "number" && Number.isFinite(raw) ? raw : null;
}

export async function retryPaymentSettlement(
    db: Database,
    telegramChargeId: string,
): Promise<IdempotentCreditResult> {
    const [receipt] = await db
        .select()
        .from(paymentReceipts)
        .where(eq(paymentReceipts.telegramChargeId, telegramChargeId))
        .limit(1);
    if (!receipt) throw new Error("Payment receipt not found");
    if (
        receipt.status !== "received" &&
        receipt.status !== "settlement_failed"
    ) {
        return { balanceAfter: 0, applied: false };
    }

    const record = paymentRecordFromReceipt(receipt);
    if (record.productType === "donation") {
        return recordDonationPayment(db, record);
    }

    if (record.productType === "pack") {
        if (record.creditTarget === "chat") {
            let chatId = record.chatId;
            if (!chatId) {
                const targetTelegramChatId = targetTelegramChatIdFromMeta(
                    receipt.meta,
                );
                if (targetTelegramChatId != null) {
                    const [targetChat] = await db
                        .select({ id: chats.id })
                        .from(chats)
                        .where(eq(chats.telegramId, targetTelegramChatId))
                        .limit(1);
                    chatId = targetChat?.id ?? null;
                }
            }
            if (!chatId) {
                throw new Error("Chat pack receipt target chat not found");
            }
            return applyChatPackPayment(db, { ...record, chatId });
        }
        if (record.creditTarget === "user") {
            return applyUserPackPayment(db, record);
        }
        throw new Error("Pack receipt has unsupported credit target");
    }

    if (!receipt.productId)
        throw new Error("Subscription receipt missing plan ID");
    return applySubscriptionPayment(
        db,
        receipt.userId,
        receipt.credits,
        receipt.productId,
        receipt.telegramChargeId,
        subscriptionExpiryFromReceipt(receipt),
        {
            chatId: receipt.chatId,
            providerChargeId: receipt.providerChargeId,
            invoicePayload: receipt.invoicePayload,
            currency: receipt.currency,
            stars: receipt.stars,
        },
        receipt.meta ?? undefined,
    );
}

async function recomputeActiveSubscriptionIn(
    tx: CreditTransaction,
    userId: string,
): Promise<void> {
    const [active] = await tx
        .select({
            planId: subscriptionPeriods.planId,
            expiresAt: subscriptionPeriods.expiresAt,
        })
        .from(subscriptionPeriods)
        .where(
            and(
                eq(subscriptionPeriods.userId, userId),
                eq(subscriptionPeriods.status, "active"),
                sql`${subscriptionPeriods.expiresAt} > now()`,
            ),
        )
        .orderBy(
            desc(subscriptionPeriods.expiresAt),
            desc(subscriptionPeriods.createdAt),
        )
        .limit(1);

    await tx
        .update(users)
        .set({
            subscriptionTier: active?.planId ?? null,
            subscriptionExpiresAt: active?.expiresAt ?? null,
        })
        .where(eq(users.id, userId));
}

async function debitUserCreditsForRefund(
    tx: CreditTransaction,
    userId: string,
    amount: number,
): Promise<{ balanceAfter: number; recoveredAmount: number }> {
    const [current] = await tx
        .select({ credits: users.credits })
        .from(users)
        .where(eq(users.id, userId))
        .for("update")
        .limit(1);

    if (!current) throw new Error("User not found");

    const recoveredAmount = Math.min(current.credits, amount);
    const balanceAfter = current.credits - recoveredAmount;

    await tx
        .update(users)
        .set({ credits: balanceAfter })
        .where(eq(users.id, userId));

    return {
        balanceAfter,
        recoveredAmount,
    };
}

async function debitChatCreditsForRefund(
    tx: CreditTransaction,
    chatId: string,
    amount: number,
): Promise<{ balanceAfter: number; recoveredAmount: number }> {
    const [current] = await tx
        .select({ credits: chats.credits })
        .from(chats)
        .where(eq(chats.id, chatId))
        .for("update")
        .limit(1);

    if (!current) throw new Error("Chat not found");

    const recoveredAmount = Math.min(current.credits, amount);
    const balanceAfter = current.credits - recoveredAmount;

    await tx
        .update(chats)
        .set({ credits: balanceAfter })
        .where(eq(chats.id, chatId));

    return {
        balanceAfter,
        recoveredAmount,
    };
}

/** Reconcile a successful Telegram Stars refund against local credit balances. */
export async function reconcileStarRefund(
    db: Database,
    telegramChargeId: string,
    meta?: Record<string, unknown>,
): Promise<RefundReconciliationResult> {
    const refundKey = `refund:${telegramChargeId}`;

    return db.transaction(async (tx) => {
        const [receipt] = await tx
            .select()
            .from(paymentReceipts)
            .where(eq(paymentReceipts.telegramChargeId, telegramChargeId))
            .for("update")
            .limit(1);

        const [original] = await tx
            .select()
            .from(ledger)
            .where(
                and(
                    eq(ledger.telegramChargeId, telegramChargeId),
                    sql`${ledger.type} IN ('purchase', 'subscription')`,
                ),
            )
            .orderBy(desc(ledger.createdAt))
            .limit(1);

        if (!original && !receipt) {
            throw new Error("No local payment found for charge");
        }

        const userId = receipt?.userId ?? original?.userId;
        if (!userId) throw new Error("Refund target user missing");
        const chatId = receipt?.chatId ?? original?.chatId ?? null;
        const receiptType =
            receipt?.productType === "pack" ? "purchase" : receipt?.productType;
        const originalType = original?.type ?? receiptType ?? "purchase";
        const originalAmount =
            receipt?.credits ??
            numberFromMeta(original?.meta, "purchasedCredits") ??
            original?.amount ??
            0;
        const unsettledReceiptWithoutLedger =
            !original &&
            receipt != null &&
            (receipt.status === "received" ||
                receipt.status === "settlement_failed");
        const refundImpactAmount = unsettledReceiptWithoutLedger
            ? 0
            : originalAmount;
        const originalStars =
            receipt?.stars ?? numberFromMeta(original?.meta, "stars") ?? null;

        if (!original) {
            const [inserted] = await tx
                .insert(ledger)
                .values({
                    userId,
                    chatId,
                    type: "refund",
                    amount: 0,
                    balanceAfter: 0,
                    telegramChargeId,
                    idempotencyKey: refundKey,
                    meta: {
                        ...meta,
                        paymentReceiptId: receipt?.id ?? null,
                        originalType,
                        originalAmount: refundImpactAmount,
                        receiptCredits: originalAmount,
                        originalStars,
                        recoveredAmount: 0,
                        unrecoveredAmount: refundImpactAmount,
                    },
                })
                .onConflictDoNothing({ target: ledger.idempotencyKey })
                .returning({ id: ledger.id });

            if (inserted) {
                if (receipt) {
                    await tx
                        .update(paymentReceipts)
                        .set({ status: "refunded", refundedAt: new Date() })
                        .where(eq(paymentReceipts.id, receipt.id));
                }
                if (receipt?.productType === "subscription") {
                    await tx
                        .update(subscriptionPeriods)
                        .set({ status: "refunded", refundedAt: new Date() })
                        .where(
                            eq(
                                subscriptionPeriods.telegramChargeId,
                                telegramChargeId,
                            ),
                        );
                    await recomputeActiveSubscriptionIn(tx, userId);
                }
                if (refundImpactAmount > 0) {
                    await createCreditDebt(tx, {
                        userId,
                        chatId,
                        paymentReceiptId: receipt?.id ?? null,
                        telegramChargeId,
                        target: chatId ? "chat" : "user",
                        amount: refundImpactAmount,
                        meta: {
                            ...meta,
                            source: "refund_unrecovered",
                            originalType,
                            originalAmount: refundImpactAmount,
                            receiptCredits: originalAmount,
                        },
                    });
                }
            }

            return {
                applied: Boolean(inserted),
                target: chatId ? "chat" : "user",
                originalType,
                originalAmount: refundImpactAmount,
                recoveredAmount: 0,
                unrecoveredAmount: refundImpactAmount,
                balanceAfter: 0,
            };
        }

        const [inserted] = await tx
            .insert(ledger)
            .values({
                userId: original.userId,
                chatId: original.chatId,
                type: "refund",
                amount: 0,
                balanceAfter: 0,
                telegramChargeId,
                idempotencyKey: refundKey,
                meta: {
                    ...meta,
                    originalLedgerId: original.id,
                    paymentReceiptId: receipt?.id ?? null,
                    originalType,
                    originalAmount,
                    originalLedgerAmount: original.amount,
                    debtRecovered:
                        numberFromMeta(original.meta, "debtRecovered") ?? 0,
                },
            })
            .onConflictDoNothing({ target: ledger.idempotencyKey })
            .returning({ id: ledger.id });

        if (!inserted) {
            const [existing] = await tx
                .select({
                    amount: ledger.amount,
                    balanceAfter: ledger.balanceAfter,
                    meta: ledger.meta,
                })
                .from(ledger)
                .where(eq(ledger.idempotencyKey, refundKey))
                .limit(1);

            const recoveredAmount =
                typeof existing?.meta?.recoveredAmount === "number"
                    ? existing.meta.recoveredAmount
                    : Math.abs(existing?.amount ?? 0);
            const originalAmount =
                typeof existing?.meta?.originalAmount === "number"
                    ? existing.meta.originalAmount
                    : (receipt?.credits ?? original.amount);

            return {
                applied: false,
                target: chatId ? "chat" : "user",
                originalType,
                originalAmount,
                recoveredAmount,
                unrecoveredAmount: Math.max(
                    0,
                    originalAmount - recoveredAmount,
                ),
                balanceAfter: existing?.balanceAfter ?? 0,
            };
        }

        const debit = chatId
            ? await debitChatCreditsForRefund(tx, chatId, originalAmount)
            : await debitUserCreditsForRefund(tx, userId, originalAmount);
        const unrecoveredAmount = Math.max(
            0,
            originalAmount - debit.recoveredAmount,
        );

        await tx
            .update(ledger)
            .set({
                amount: -debit.recoveredAmount,
                balanceAfter: debit.balanceAfter,
                meta: {
                    ...meta,
                    originalLedgerId: original.id,
                    paymentReceiptId: receipt?.id ?? null,
                    originalType,
                    originalAmount,
                    originalLedgerAmount: original.amount,
                    debtRecovered:
                        numberFromMeta(original.meta, "debtRecovered") ?? 0,
                    recoveredAmount: debit.recoveredAmount,
                    unrecoveredAmount,
                },
            })
            .where(eq(ledger.id, inserted.id));

        if (receipt) {
            await tx
                .update(paymentReceipts)
                .set({ status: "refunded", refundedAt: new Date() })
                .where(eq(paymentReceipts.id, receipt.id));
        }

        if (
            originalType === "subscription" ||
            receipt?.productType === "subscription"
        ) {
            await tx
                .update(subscriptionPeriods)
                .set({ status: "refunded", refundedAt: new Date() })
                .where(
                    eq(subscriptionPeriods.telegramChargeId, telegramChargeId),
                );
            await recomputeActiveSubscriptionIn(tx, userId);
        }

        if (unrecoveredAmount > 0) {
            await createCreditDebt(tx, {
                userId,
                chatId,
                paymentReceiptId: receipt?.id ?? null,
                telegramChargeId,
                target: chatId ? "chat" : "user",
                amount: unrecoveredAmount,
                meta: {
                    ...meta,
                    source: "refund_unrecovered",
                    originalLedgerId: original.id,
                    originalType,
                    originalAmount,
                },
            });
        }

        return {
            applied: true,
            target: chatId ? "chat" : "user",
            originalType,
            originalAmount,
            recoveredAmount: debit.recoveredAmount,
            unrecoveredAmount,
            balanceAfter: debit.balanceAfter,
        };
    });
}
