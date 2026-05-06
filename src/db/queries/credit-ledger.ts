import { and, eq, sql } from "drizzle-orm";
import type { Database } from "../connection";
import { chats, creditDebts, ledger, users } from "../schema";

export interface IdempotentCreditResult {
    balanceAfter: number;
    applied: boolean;
    ledgerId?: string;
    debtRecovered?: number;
    creditedAmount?: number;
}

/** Get both user and chat credit balances. */
export async function getBalances(
    db: Database,
    userTelegramId: number,
    chatTelegramId: number,
): Promise<{ userCredits: number; chatCredits: number }> {
    const [userRow] = await db
        .select({ credits: users.credits })
        .from(users)
        .where(eq(users.telegramId, userTelegramId))
        .limit(1);

    const [chatRow] = await db
        .select({ credits: chats.credits })
        .from(chats)
        .where(eq(chats.telegramId, chatTelegramId))
        .limit(1);

    return {
        userCredits: userRow?.credits ?? 0,
        chatCredits: chatRow?.credits ?? 0,
    };
}

export async function markLedgerSpendStatus(
    db: Database,
    ledgerId: string | undefined,
    status:
        | "reserved"
        | "provider_succeeded"
        | "delivered"
        | "delivery_failed"
        | "refunded"
        | "billable_failure",
    meta: Record<string, unknown> = {},
): Promise<void> {
    if (!ledgerId) return;
    await db
        .update(ledger)
        .set({
            meta: sql`COALESCE(${ledger.meta}, '{}'::jsonb) || ${JSON.stringify(
                {
                    ...meta,
                    spendStatus: status,
                    spendStatusUpdatedAt: new Date().toISOString(),
                },
            )}::jsonb`,
        })
        .where(eq(ledger.id, ledgerId));
}

function noOpenUserDebt(userId: string) {
    return sql`NOT EXISTS (
        SELECT 1 FROM ${creditDebts}
        WHERE ${creditDebts.userId} = ${userId}
            AND ${creditDebts.chatId} IS NULL
            AND ${creditDebts.status} = 'open'
    )`;
}

function noOpenChatDebt(chatId: string) {
    return sql`NOT EXISTS (
        SELECT 1 FROM ${creditDebts}
        WHERE ${creditDebts.chatId} = ${chatId}
            AND ${creditDebts.status} = 'open'
    )`;
}

/** Deduct credits from user balance and record in ledger atomically. */
export async function deductUserCredits(
    db: Database,
    userId: string,
    amount: number,
    toolName: string,
    modelId: string | null,
    idempotencyKey?: string,
    meta?: Record<string, unknown>,
): Promise<IdempotentCreditResult> {
    return db.transaction(async (tx) => {
        if (idempotencyKey) {
            const [inserted] = await tx
                .insert(ledger)
                .values({
                    userId,
                    type: "spend",
                    amount: -amount,
                    balanceAfter: 0,
                    toolName,
                    modelId,
                    idempotencyKey,
                    meta,
                })
                .onConflictDoNothing({ target: ledger.idempotencyKey })
                .returning({ id: ledger.id });

            if (!inserted) {
                const [existing] = await tx
                    .select({
                        id: ledger.id,
                        balanceAfter: ledger.balanceAfter,
                    })
                    .from(ledger)
                    .where(eq(ledger.idempotencyKey, idempotencyKey))
                    .limit(1);
                return {
                    balanceAfter: existing?.balanceAfter ?? 0,
                    applied: false,
                    ledgerId: existing?.id,
                };
            }

            const [updated] = await tx
                .update(users)
                .set({
                    credits: sql`${users.credits} - ${amount}`,
                })
                .where(
                    and(
                        eq(users.id, userId),
                        sql`${users.credits} >= ${amount}`,
                        noOpenUserDebt(userId),
                    ),
                )
                .returning({ credits: users.credits });

            if (!updated)
                throw new Error("Insufficient credits or open refund debt");

            await tx
                .update(ledger)
                .set({ balanceAfter: updated.credits })
                .where(eq(ledger.id, inserted.id));

            return {
                balanceAfter: updated.credits,
                applied: true,
                ledgerId: inserted.id,
            };
        }

        const [updated] = await tx
            .update(users)
            .set({
                credits: sql`${users.credits} - ${amount}`,
            })
            .where(
                and(
                    eq(users.id, userId),
                    sql`${users.credits} >= ${amount}`,
                    noOpenUserDebt(userId),
                ),
            )
            .returning({ credits: users.credits });

        if (!updated)
            throw new Error("Insufficient credits or open refund debt");

        const [inserted] = await tx
            .insert(ledger)
            .values({
                userId,
                type: "spend",
                amount: -amount,
                balanceAfter: updated.credits,
                toolName,
                modelId,
                idempotencyKey,
                meta,
            })
            .returning({ id: ledger.id });

        return {
            balanceAfter: updated.credits,
            applied: true,
            ledgerId: inserted?.id,
        };
    });
}

/** Deduct credits from chat pool and record in ledger atomically. */
export async function deductChatCredits(
    db: Database,
    chatId: string,
    userId: string,
    amount: number,
    toolName: string,
    modelId: string | null,
    idempotencyKey?: string,
    meta?: Record<string, unknown>,
): Promise<IdempotentCreditResult> {
    return db.transaction(async (tx) => {
        if (idempotencyKey) {
            const [inserted] = await tx
                .insert(ledger)
                .values({
                    userId,
                    chatId,
                    type: "spend",
                    amount: -amount,
                    balanceAfter: 0,
                    toolName,
                    modelId,
                    idempotencyKey,
                    meta,
                })
                .onConflictDoNothing({ target: ledger.idempotencyKey })
                .returning({ id: ledger.id });

            if (!inserted) {
                const [existing] = await tx
                    .select({
                        id: ledger.id,
                        balanceAfter: ledger.balanceAfter,
                    })
                    .from(ledger)
                    .where(eq(ledger.idempotencyKey, idempotencyKey))
                    .limit(1);
                return {
                    balanceAfter: existing?.balanceAfter ?? 0,
                    applied: false,
                    ledgerId: existing?.id,
                };
            }

            const [updated] = await tx
                .update(chats)
                .set({
                    credits: sql`${chats.credits} - ${amount}`,
                })
                .where(
                    and(
                        eq(chats.id, chatId),
                        sql`${chats.credits} >= ${amount}`,
                        noOpenChatDebt(chatId),
                    ),
                )
                .returning({ credits: chats.credits });

            if (!updated) {
                throw new Error(
                    "Insufficient chat credits or open group refund debt",
                );
            }

            await tx
                .update(ledger)
                .set({ balanceAfter: updated.credits })
                .where(eq(ledger.id, inserted.id));

            return {
                balanceAfter: updated.credits,
                applied: true,
                ledgerId: inserted.id,
            };
        }

        const [updated] = await tx
            .update(chats)
            .set({
                credits: sql`${chats.credits} - ${amount}`,
            })
            .where(
                and(
                    eq(chats.id, chatId),
                    sql`${chats.credits} >= ${amount}`,
                    noOpenChatDebt(chatId),
                ),
            )
            .returning({ credits: chats.credits });

        if (!updated) {
            throw new Error(
                "Insufficient chat credits or open group refund debt",
            );
        }

        const [inserted] = await tx
            .insert(ledger)
            .values({
                userId,
                chatId,
                type: "spend",
                amount: -amount,
                balanceAfter: updated.credits,
                toolName,
                modelId,
                idempotencyKey,
                meta,
            })
            .returning({ id: ledger.id });

        return {
            balanceAfter: updated.credits,
            applied: true,
            ledgerId: inserted?.id,
        };
    });
}

/** Add credits to user and record in ledger atomically. */
export async function addUserCreditsWithResult(
    db: Database,
    userId: string,
    amount: number,
    type: string,
    telegramChargeId?: string,
    idempotencyKey?: string,
    meta?: Record<string, unknown>,
): Promise<IdempotentCreditResult> {
    return db.transaction(async (tx) => {
        if (idempotencyKey) {
            const [inserted] = await tx
                .insert(ledger)
                .values({
                    userId,
                    type,
                    amount,
                    balanceAfter: 0,
                    telegramChargeId,
                    idempotencyKey,
                    meta,
                })
                .onConflictDoNothing({ target: ledger.idempotencyKey })
                .returning({ id: ledger.id });

            if (!inserted) {
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

            const [updated] = await tx
                .update(users)
                .set({
                    credits: sql`${users.credits} + ${amount}`,
                })
                .where(eq(users.id, userId))
                .returning({ credits: users.credits });

            if (!updated) throw new Error("User not found");

            await tx
                .update(ledger)
                .set({ balanceAfter: updated.credits })
                .where(eq(ledger.id, inserted.id));

            return { balanceAfter: updated.credits, applied: true };
        }

        const [updated] = await tx
            .update(users)
            .set({
                credits: sql`${users.credits} + ${amount}`,
            })
            .where(eq(users.id, userId))
            .returning({ credits: users.credits });

        if (!updated) throw new Error("User not found");

        await tx.insert(ledger).values({
            userId,
            type,
            amount,
            balanceAfter: updated.credits,
            telegramChargeId,
            idempotencyKey,
            meta,
        });

        return { balanceAfter: updated.credits, applied: true };
    });
}

/** Add credits to user and return the updated balance. */
export async function addUserCredits(
    db: Database,
    userId: string,
    amount: number,
    type: string,
    telegramChargeId?: string,
    idempotencyKey?: string,
    meta?: Record<string, unknown>,
): Promise<number> {
    const result = await addUserCreditsWithResult(
        db,
        userId,
        amount,
        type,
        telegramChargeId,
        idempotencyKey,
        meta,
    );
    return result.balanceAfter;
}

/** Add credits to chat pool and record in ledger atomically. */
export async function addChatCreditsWithResult(
    db: Database,
    chatId: string,
    userId: string,
    amount: number,
    type: string,
    telegramChargeId?: string,
    idempotencyKey?: string,
    meta?: Record<string, unknown>,
): Promise<IdempotentCreditResult> {
    return db.transaction(async (tx) => {
        if (idempotencyKey) {
            const [inserted] = await tx
                .insert(ledger)
                .values({
                    userId,
                    chatId,
                    type,
                    amount,
                    balanceAfter: 0,
                    telegramChargeId,
                    idempotencyKey,
                    meta,
                })
                .onConflictDoNothing({ target: ledger.idempotencyKey })
                .returning({ id: ledger.id });

            if (!inserted) {
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

            const [updated] = await tx
                .update(chats)
                .set({
                    credits: sql`${chats.credits} + ${amount}`,
                })
                .where(eq(chats.id, chatId))
                .returning({ credits: chats.credits });

            if (!updated) throw new Error("Chat not found");

            await tx
                .update(ledger)
                .set({ balanceAfter: updated.credits })
                .where(eq(ledger.id, inserted.id));

            return { balanceAfter: updated.credits, applied: true };
        }

        const [updated] = await tx
            .update(chats)
            .set({
                credits: sql`${chats.credits} + ${amount}`,
            })
            .where(eq(chats.id, chatId))
            .returning({ credits: chats.credits });

        if (!updated) throw new Error("Chat not found");

        await tx.insert(ledger).values({
            userId,
            chatId,
            type,
            amount,
            balanceAfter: updated.credits,
            telegramChargeId,
            idempotencyKey,
            meta,
        });

        return { balanceAfter: updated.credits, applied: true };
    });
}

/** Add credits to chat pool and return the updated balance. */
export async function addChatCredits(
    db: Database,
    chatId: string,
    userId: string,
    amount: number,
    type: string,
    telegramChargeId?: string,
    idempotencyKey?: string,
    meta?: Record<string, unknown>,
): Promise<number> {
    const result = await addChatCreditsWithResult(
        db,
        chatId,
        userId,
        amount,
        type,
        telegramChargeId,
        idempotencyKey,
        meta,
    );
    return result.balanceAfter;
}

/** Find a transaction by idempotency key for deduplication. */
export async function getTransactionByIdempotencyKey(
    db: Database,
    key: string,
): Promise<typeof ledger.$inferSelect | null> {
    const [row] = await db
        .select()
        .from(ledger)
        .where(eq(ledger.idempotencyKey, key))
        .limit(1);
    return row ?? null;
}
