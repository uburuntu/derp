import { and, eq, sql } from "drizzle-orm";
import type { Database } from "../connection";
import { ledger, usageQuotas, users } from "../schema";

export type ToolDebitResult = "applied" | "duplicate" | "quota_exhausted";

/** Get daily usage count for a specific tool. */
export async function getDailyUsage(
    db: Database,
    userId: string,
    chatId: string,
    toolName: string,
): Promise<number> {
    const today = new Date().toISOString().slice(0, 10);
    const [row] = await db
        .select({ usage: usageQuotas.usage })
        .from(usageQuotas)
        .where(
            and(
                eq(usageQuotas.userId, userId),
                eq(usageQuotas.chatId, chatId),
                eq(usageQuotas.usageDate, today),
            ),
        )
        .limit(1);

    if (!row?.usage) return 0;
    return (row.usage as Record<string, number>)[toolName] ?? 0;
}

async function incrementDailyUsageIn(
    db: Pick<Database, "insert">,
    userId: string,
    chatId: string,
    toolName: string,
): Promise<void> {
    const today = new Date().toISOString().slice(0, 10);

    await db
        .insert(usageQuotas)
        .values({
            userId,
            chatId,
            usageDate: today,
            usage: { [toolName]: 1 },
        })
        .onConflictDoUpdate({
            target: [
                usageQuotas.userId,
                usageQuotas.chatId,
                usageQuotas.usageDate,
            ],
            set: {
                usage: sql`jsonb_set(
                    COALESCE(${usageQuotas.usage}, '{}'),
                    ${`{${toolName}}`},
                    (COALESCE((${usageQuotas.usage}->${toolName})::int, 0) + 1)::text::jsonb
                )`,
            },
        });
}

class QuotaExhaustedError extends Error {
    constructor() {
        super("Daily free quota exhausted");
    }
}

async function reserveDailyUsageIn(
    db: Pick<Database, "execute">,
    userId: string,
    chatId: string,
    toolName: string,
    limit: number,
): Promise<void> {
    const today = new Date().toISOString().slice(0, 10);
    const rows = await db.execute(sql`
        INSERT INTO ${usageQuotas} (
            ${usageQuotas.userId},
            ${usageQuotas.chatId},
            ${usageQuotas.usageDate},
            ${usageQuotas.usage}
        )
        VALUES (
            ${userId},
            ${chatId},
            ${today},
            jsonb_build_object(${toolName}, 1)
        )
        ON CONFLICT (
            ${usageQuotas.userId},
            ${usageQuotas.chatId},
            ${usageQuotas.usageDate}
        )
        DO UPDATE SET
            ${usageQuotas.usage} = jsonb_set(
                COALESCE(${usageQuotas.usage}, '{}'::jsonb),
                ARRAY[${toolName}]::text[],
                to_jsonb(COALESCE((${usageQuotas.usage}->>${toolName})::int, 0) + 1),
                true
            ),
            ${usageQuotas.updatedAt} = now()
        WHERE COALESCE((${usageQuotas.usage}->>${toolName})::int, 0) < ${limit}
        RETURNING ${usageQuotas.usage}
    `);

    if (rows.length === 0) {
        throw new QuotaExhaustedError();
    }
}

/** Increment daily usage for a tool. */
export async function incrementDailyUsage(
    db: Database,
    userId: string,
    chatId: string,
    toolName: string,
): Promise<void> {
    await incrementDailyUsageIn(db, userId, chatId, toolName);
}

/** Record an idempotent free tool use and increment its daily quota. */
export async function recordFreeToolUsage(
    db: Database,
    userId: string,
    chatId: string,
    toolName: string,
    modelId: string | null,
    freeDailyLimit: number,
    idempotencyKey?: string,
    meta?: Record<string, unknown>,
): Promise<ToolDebitResult> {
    if (!idempotencyKey) {
        try {
            await reserveDailyUsageIn(
                db,
                userId,
                chatId,
                toolName,
                freeDailyLimit,
            );
            return "applied";
        } catch (err) {
            if (err instanceof QuotaExhaustedError) return "quota_exhausted";
            throw err;
        }
    }

    try {
        return await db.transaction(async (tx) => {
            const [inserted] = await tx
                .insert(ledger)
                .values({
                    userId,
                    chatId,
                    type: "spend",
                    amount: 0,
                    balanceAfter: 0,
                    toolName,
                    modelId,
                    idempotencyKey,
                    meta,
                })
                .onConflictDoNothing({ target: ledger.idempotencyKey })
                .returning({ id: ledger.id });

            if (!inserted) return "duplicate";

            await reserveDailyUsageIn(
                tx,
                userId,
                chatId,
                toolName,
                freeDailyLimit,
            );

            const [userRow] = await tx
                .select({ credits: users.credits })
                .from(users)
                .where(eq(users.id, userId))
                .limit(1);

            await tx
                .update(ledger)
                .set({ balanceAfter: userRow?.credits ?? 0 })
                .where(eq(ledger.id, inserted.id));

            return "applied";
        });
    } catch (err) {
        if (err instanceof QuotaExhaustedError) return "quota_exhausted";
        throw err;
    }
}
