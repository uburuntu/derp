import { sql } from "drizzle-orm";
import {
    bigint,
    boolean,
    check,
    date,
    index,
    integer,
    jsonb,
    pgTable,
    text,
    timestamp,
    unique,
    uniqueIndex,
    uuid,
    varchar,
} from "drizzle-orm/pg-core";

// ── Users ────────────────────────────────────────────────────────────────────

export interface UserPreferences {
    responseStyle?: "concise" | "balanced" | "detailed";
    customInstructions?: string | null;
    disabledTools?: string[];
}

export const users = pgTable(
    "users",
    {
        id: uuid("id").primaryKey().defaultRandom(),
        telegramId: bigint("telegram_id", { mode: "number" }).notNull(),
        isBot: boolean("is_bot").notNull().default(false),
        firstName: varchar("first_name", { length: 255 }).notNull(),
        lastName: varchar("last_name", { length: 255 }),
        username: varchar("username", { length: 255 }),
        languageCode: varchar("language_code", { length: 10 }),
        isPremium: boolean("is_premium").notNull().default(false),
        preferences: jsonb("preferences")
            .$type<UserPreferences>()
            .default({ responseStyle: "balanced" }),
        credits: integer("credits").notNull().default(0),
        subscriptionTier: varchar("subscription_tier", { length: 10 }), // 'lite', 'pro', 'ultra', or null
        subscriptionExpiresAt: timestamp("subscription_expires_at", {
            withTimezone: true,
        }),
        createdAt: timestamp("created_at", { withTimezone: true })
            .notNull()
            .defaultNow(),
        updatedAt: timestamp("updated_at", { withTimezone: true })
            .notNull()
            .defaultNow()
            .$onUpdate(() => new Date()),
    },
    (t) => [
        unique("users_telegram_id_unique").on(t.telegramId),
        index("users_telegram_id_idx").on(t.telegramId),
        check("users_credits_check", sql`${t.credits} >= 0`),
    ],
);

// ── Chats ────────────────────────────────────────────────────────────────────

export const chats = pgTable(
    "chats",
    {
        id: uuid("id").primaryKey().defaultRandom(),
        telegramId: bigint("telegram_id", { mode: "number" }).notNull(),
        type: varchar("type", { length: 20 }).notNull(), // private, group, supergroup, channel
        title: varchar("title", { length: 255 }),
        username: varchar("username", { length: 255 }),
        firstName: varchar("first_name", { length: 255 }),
        lastName: varchar("last_name", { length: 255 }),
        isForum: boolean("is_forum").notNull().default(false),
        description: text("description"), // cached from getChat
        memory: text("memory"), // chat memory, max 4096 enforced in app
        personality: varchar("personality", { length: 20 }).default("default"), // default, professional, casual, creative, custom
        customPrompt: text("custom_prompt"), // subscribers-only override
        settings: jsonb("settings").$type<ChatSettings>().default({
            memoryAccess: "admins",
            remindersAccess: "admins",
        }),
        credits: integer("credits").notNull().default(0),
        languageCode: varchar("language_code", { length: 10 }),
        cachedAt: timestamp("cached_at", { withTimezone: true }),
        createdAt: timestamp("created_at", { withTimezone: true })
            .notNull()
            .defaultNow(),
        updatedAt: timestamp("updated_at", { withTimezone: true })
            .notNull()
            .defaultNow()
            .$onUpdate(() => new Date()),
    },
    (t) => [
        unique("chats_telegram_id_unique").on(t.telegramId),
        index("chats_telegram_id_idx").on(t.telegramId),
        check("chats_credits_check", sql`${t.credits} >= 0`),
    ],
);

export interface ChatSettings {
    memoryAccess: "admins" | "everyone";
    remindersAccess: "admins" | "everyone";
}

export type CreditAccountTarget = "user" | "chat";

// ── Chat Members ─────────────────────────────────────────────────────────────

export const chatMembers = pgTable(
    "chat_members",
    {
        id: uuid("id").primaryKey().defaultRandom(),
        chatId: uuid("chat_id")
            .notNull()
            .references(() => chats.id),
        userId: uuid("user_id")
            .notNull()
            .references(() => users.id),
        role: varchar("role", { length: 20 }).notNull().default("member"), // creator, administrator, member, restricted, left, kicked
        customTitle: varchar("custom_title", { length: 255 }),
        bio: varchar("bio", { length: 255 }), // cached from getChatMember
        isActive: boolean("is_active").notNull().default(true),
        cachedAt: timestamp("cached_at", { withTimezone: true }),
        lastSeenAt: timestamp("last_seen_at", { withTimezone: true }),
        createdAt: timestamp("created_at", { withTimezone: true })
            .notNull()
            .defaultNow(),
        updatedAt: timestamp("updated_at", { withTimezone: true })
            .notNull()
            .defaultNow()
            .$onUpdate(() => new Date()),
    },
    (t) => [
        unique("chat_members_chat_user_unique").on(t.chatId, t.userId),
        index("chat_members_chat_id_idx").on(t.chatId),
        index("chat_members_user_id_idx").on(t.userId),
    ],
);

// ── Messages ─────────────────────────────────────────────────────────────────

export const messages = pgTable(
    "messages",
    {
        id: uuid("id").primaryKey().defaultRandom(),
        chatId: uuid("chat_id")
            .notNull()
            .references(() => chats.id),
        userId: uuid("user_id").references(() => users.id), // null for bot's own messages if no user row
        telegramMessageId: integer("telegram_message_id").notNull(),
        threadId: integer("thread_id"), // forum topic ID
        direction: varchar("direction", { length: 3 }).notNull(), // 'in' or 'out'
        contentType: varchar("content_type", { length: 20 }), // text, photo, video, voice, etc.
        text: text("text"),
        mediaGroupId: varchar("media_group_id", { length: 50 }),
        attachmentType: varchar("attachment_type", { length: 20 }),
        attachmentFileId: varchar("attachment_file_id", { length: 255 }),
        replyToMessageId: integer("reply_to_message_id"),
        metadata: jsonb("metadata").$type<MessageMetadata>(), // tools used, credits, tokens, model, duration — for bot responses
        telegramDate: timestamp("telegram_date", {
            withTimezone: true,
        }).notNull(),
        editedAt: timestamp("edited_at", { withTimezone: true }),
        deletedAt: timestamp("deleted_at", { withTimezone: true }), // soft delete
        createdAt: timestamp("created_at", { withTimezone: true })
            .notNull()
            .defaultNow(),
        updatedAt: timestamp("updated_at", { withTimezone: true })
            .notNull()
            .defaultNow()
            .$onUpdate(() => new Date()),
    },
    (t) => [
        unique("messages_chat_msg_unique").on(t.chatId, t.telegramMessageId),
        index("messages_chat_id_idx").on(t.chatId),
        index("messages_chat_date_idx").on(t.chatId, t.telegramDate),
        index("messages_chat_thread_date_idx").on(
            t.chatId,
            t.threadId,
            t.telegramDate,
        ),
    ],
);

export interface MessageMetadata {
    model?: string;
    tier?: string;
    inputTokens?: number;
    outputTokens?: number;
    cacheHitTokens?: number;
    toolsUsed?: string[];
    creditsSpent?: number;
    creditSource?: string; // "user" | "chat" | "personal" | "group" | "free"
    providerCallIds?: string[];
    costMicros?: number;
    providerRoute?: "primary" | "fallback";
    fallbackFrom?: string;
    durationMs?: number;
}

// ── Ledger (credit transactions) ─────────────────────────────────────────────

export const ledger = pgTable(
    "ledger",
    {
        id: uuid("id").primaryKey().defaultRandom(),
        userId: uuid("user_id")
            .notNull()
            .references(() => users.id),
        chatId: uuid("chat_id").references(() => chats.id), // null for user-only transactions
        type: varchar("type", { length: 20 }).notNull(), // purchase, spend, refund, grant, subscription, donation
        amount: integer("amount").notNull(), // positive = credit in, negative = credit out
        balanceAfter: integer("balance_after").notNull(),
        toolName: varchar("tool_name", { length: 50 }),
        modelId: varchar("model_id", { length: 100 }),
        telegramChargeId: varchar("telegram_charge_id", { length: 255 }),
        description: varchar("description", { length: 255 }),
        idempotencyKey: varchar("idempotency_key", { length: 255 }),
        meta: jsonb("meta").$type<Record<string, unknown>>(),
        createdAt: timestamp("created_at", { withTimezone: true })
            .notNull()
            .defaultNow(),
    },
    (t) => [
        unique("ledger_idempotency_key_unique").on(t.idempotencyKey),
        index("ledger_user_id_idx").on(t.userId),
        index("ledger_chat_id_idx").on(t.chatId),
        uniqueIndex("ledger_payment_receipt_charge_unique")
            .on(t.telegramChargeId)
            .where(
                sql`${t.telegramChargeId} IS NOT NULL AND ${t.amount} > 0 AND ${t.type} IN ('purchase', 'subscription')`,
            ),
    ],
);

// ── Payment Receipts (durable Stars accounting) ─────────────────────────────

export const paymentReceipts = pgTable(
    "payment_receipts",
    {
        id: uuid("id").primaryKey().defaultRandom(),
        userId: uuid("user_id")
            .notNull()
            .references(() => users.id),
        chatId: uuid("chat_id").references(() => chats.id),
        telegramChargeId: varchar("telegram_charge_id", {
            length: 255,
        }).notNull(),
        providerChargeId: varchar("provider_charge_id", { length: 255 }),
        invoicePayload: varchar("invoice_payload", { length: 255 }),
        currency: varchar("currency", { length: 10 }).notNull().default("XTR"),
        stars: integer("stars").notNull(),
        productType: varchar("product_type", { length: 30 }).notNull(), // subscription, pack, donation
        productId: varchar("product_id", { length: 100 }),
        creditTarget: varchar("credit_target", { length: 20 }), // user, chat, none
        credits: integer("credits").notNull().default(0),
        status: varchar("status", { length: 30 }).notNull().default("settled"), // received, settled, settlement_failed, refund_pending, refunded
        settledAt: timestamp("settled_at", { withTimezone: true }),
        refundedAt: timestamp("refunded_at", { withTimezone: true }),
        meta: jsonb("meta").$type<Record<string, unknown>>(),
        createdAt: timestamp("created_at", { withTimezone: true })
            .notNull()
            .defaultNow(),
        updatedAt: timestamp("updated_at", { withTimezone: true })
            .notNull()
            .defaultNow()
            .$onUpdate(() => new Date()),
    },
    (t) => [
        uniqueIndex("payment_receipts_telegram_charge_unique").on(
            t.telegramChargeId,
        ),
        index("payment_receipts_user_id_idx").on(t.userId),
        index("payment_receipts_chat_id_idx").on(t.chatId),
        index("payment_receipts_status_idx").on(t.status),
    ],
);

// ── Provider Calls (durable model/provider spend accounting) ────────────────

export const providerCalls = pgTable(
    "provider_calls",
    {
        id: uuid("id").primaryKey().defaultRandom(),
        logicalRequestKey: varchar("logical_request_key", { length: 255 }),
        attemptNo: integer("attempt_no").notNull().default(1),
        provider: varchar("provider", { length: 40 }).notNull(),
        operation: varchar("operation", { length: 40 }).notNull(), // chat, chat_with_tools, image, video, tts, inline, reminder
        route: varchar("route", { length: 40 }).notNull().default("primary"), // primary, fallback
        keyClass: varchar("key_class", { length: 20 })
            .notNull()
            .default("free"), // free, paid
        modelId: varchar("model_id", { length: 150 }).notNull(),
        actualModelId: varchar("actual_model_id", { length: 150 }),
        userId: uuid("user_id").references(() => users.id),
        chatId: uuid("chat_id").references(() => chats.id),
        ledgerId: uuid("ledger_id").references(() => ledger.id),
        messageId: uuid("message_id").references(() => messages.id),
        toolName: varchar("tool_name", { length: 50 }),
        status: varchar("status", { length: 30 }).notNull().default("started"), // started, succeeded, failed
        inputTokens: integer("input_tokens").notNull().default(0),
        outputTokens: integer("output_tokens").notNull().default(0),
        cacheHitTokens: integer("cache_hit_tokens").notNull().default(0),
        mediaInputCount: integer("media_input_count").notNull().default(0),
        mediaOutputCount: integer("media_output_count").notNull().default(0),
        durationSeconds: integer("duration_seconds"),
        estimatedCostMicros: integer("estimated_cost_micros")
            .notNull()
            .default(0),
        actualCostMicros: integer("actual_cost_micros").notNull().default(0),
        creditsCharged: integer("credits_charged").notNull().default(0),
        creditSource: varchar("credit_source", { length: 20 }),
        providerRequestId: varchar("provider_request_id", { length: 255 }),
        finishReason: varchar("finish_reason", { length: 100 }),
        errorCode: varchar("error_code", { length: 100 }),
        errorMessage: varchar("error_message", { length: 500 }),
        meta: jsonb("meta").$type<Record<string, unknown>>(),
        startedAt: timestamp("started_at", { withTimezone: true })
            .notNull()
            .defaultNow(),
        finishedAt: timestamp("finished_at", { withTimezone: true }),
        createdAt: timestamp("created_at", { withTimezone: true })
            .notNull()
            .defaultNow(),
        updatedAt: timestamp("updated_at", { withTimezone: true })
            .notNull()
            .defaultNow()
            .$onUpdate(() => new Date()),
    },
    (t) => [
        unique("provider_calls_logical_attempt_unique").on(
            t.logicalRequestKey,
            t.attemptNo,
        ),
        index("provider_calls_user_id_idx").on(t.userId),
        index("provider_calls_chat_id_idx").on(t.chatId),
        index("provider_calls_provider_model_idx").on(t.provider, t.modelId),
        index("provider_calls_status_idx").on(t.status),
        index("provider_calls_created_at_idx").on(t.createdAt),
    ],
);

// ── Refund Debts ────────────────────────────────────────────────────────────

export const creditDebts = pgTable(
    "credit_debts",
    {
        id: uuid("id").primaryKey().defaultRandom(),
        userId: uuid("user_id")
            .notNull()
            .references(() => users.id),
        chatId: uuid("chat_id").references(() => chats.id),
        paymentReceiptId: uuid("payment_receipt_id").references(
            () => paymentReceipts.id,
        ),
        telegramChargeId: varchar("telegram_charge_id", { length: 255 }),
        target: varchar("target", { length: 20 }).notNull(), // user, chat
        status: varchar("status", { length: 20 }).notNull().default("open"), // open, settled, waived
        amount: integer("amount").notNull(),
        recoveredAmount: integer("recovered_amount").notNull().default(0),
        outstandingAmount: integer("outstanding_amount").notNull(),
        meta: jsonb("meta").$type<Record<string, unknown>>(),
        settledAt: timestamp("settled_at", { withTimezone: true }),
        createdAt: timestamp("created_at", { withTimezone: true })
            .notNull()
            .defaultNow(),
        updatedAt: timestamp("updated_at", { withTimezone: true })
            .notNull()
            .defaultNow()
            .$onUpdate(() => new Date()),
    },
    (t) => [
        index("credit_debts_user_status_idx").on(t.userId, t.status),
        index("credit_debts_chat_status_idx").on(t.chatId, t.status),
        index("credit_debts_charge_idx").on(t.telegramChargeId),
        check("credit_debts_amount_check", sql`${t.amount} > 0`),
        check(
            "credit_debts_outstanding_check",
            sql`${t.outstandingAmount} >= 0`,
        ),
    ],
);

export const creditDebtEvents = pgTable(
    "credit_debt_events",
    {
        id: uuid("id").primaryKey().defaultRandom(),
        debtId: uuid("debt_id")
            .notNull()
            .references(() => creditDebts.id),
        userId: uuid("user_id")
            .notNull()
            .references(() => users.id),
        chatId: uuid("chat_id").references(() => chats.id),
        ledgerId: uuid("ledger_id").references(() => ledger.id),
        paymentReceiptId: uuid("payment_receipt_id").references(
            () => paymentReceipts.id,
        ),
        type: varchar("type", { length: 20 }).notNull(), // created, recovered, waived
        amount: integer("amount").notNull(),
        meta: jsonb("meta").$type<Record<string, unknown>>(),
        createdAt: timestamp("created_at", { withTimezone: true })
            .notNull()
            .defaultNow(),
    },
    (t) => [
        index("credit_debt_events_debt_id_idx").on(t.debtId),
        index("credit_debt_events_user_id_idx").on(t.userId),
        index("credit_debt_events_chat_id_idx").on(t.chatId),
    ],
);

// ── Quota Windows (global trial and promo burn accounting) ──────────────────

export const quotaWindows = pgTable(
    "quota_windows",
    {
        id: uuid("id").primaryKey().defaultRandom(),
        scope: varchar("scope", { length: 40 }).notNull(), // free_chat, web_search, inline, promo_media, bot_promo
        subjectKey: varchar("subject_key", { length: 120 }).notNull(), // user:<uuid>, chat:<uuid>, bot
        userId: uuid("user_id").references(() => users.id),
        chatId: uuid("chat_id").references(() => chats.id),
        windowKey: varchar("window_key", { length: 40 }).notNull(), // YYYY-MM-DD, YYYY-Www, global
        used: integer("used").notNull().default(0),
        limit: integer("limit").notNull(),
        meta: jsonb("meta").$type<Record<string, unknown>>(),
        createdAt: timestamp("created_at", { withTimezone: true })
            .notNull()
            .defaultNow(),
        updatedAt: timestamp("updated_at", { withTimezone: true })
            .notNull()
            .defaultNow()
            .$onUpdate(() => new Date()),
    },
    (t) => [
        unique("quota_windows_scope_user_chat_window_unique").on(
            t.scope,
            t.subjectKey,
            t.windowKey,
        ),
        index("quota_windows_scope_window_idx").on(t.scope, t.windowKey),
        index("quota_windows_subject_idx").on(t.subjectKey),
        index("quota_windows_user_scope_idx").on(t.userId, t.scope),
    ],
);

// ── Pending Tool Confirmations ──────────────────────────────────────────────

export const pendingToolConfirmations = pgTable(
    "pending_tool_confirmations",
    {
        id: uuid("id").primaryKey().defaultRandom(),
        userId: uuid("user_id")
            .notNull()
            .references(() => users.id),
        chatId: uuid("chat_id")
            .notNull()
            .references(() => chats.id),
        telegramChatId: bigint("telegram_chat_id", {
            mode: "number",
        }).notNull(),
        messageId: integer("message_id"),
        threadId: integer("thread_id"),
        toolName: varchar("tool_name", { length: 50 }).notNull(),
        params: jsonb("params").$type<Record<string, unknown>>().notNull(),
        cost: integer("cost").notNull(),
        source: varchar("source", { length: 20 }).notNull(), // user, chat
        status: varchar("status", { length: 20 }).notNull().default("pending"), // pending, confirmed, cancelled, expired
        idempotencyKey: varchar("idempotency_key", { length: 255 }).notNull(),
        expiresAt: timestamp("expires_at", { withTimezone: true }).notNull(),
        meta: jsonb("meta").$type<Record<string, unknown>>(),
        createdAt: timestamp("created_at", { withTimezone: true })
            .notNull()
            .defaultNow(),
        updatedAt: timestamp("updated_at", { withTimezone: true })
            .notNull()
            .defaultNow()
            .$onUpdate(() => new Date()),
    },
    (t) => [
        unique("pending_tool_confirmations_key_unique").on(t.idempotencyKey),
        index("pending_tool_confirmations_user_status_idx").on(
            t.userId,
            t.status,
        ),
        index("pending_tool_confirmations_chat_status_idx").on(
            t.chatId,
            t.status,
        ),
        index("pending_tool_confirmations_expires_idx").on(t.expiresAt),
    ],
);

// ── Subscription Periods ────────────────────────────────────────────────────

export const subscriptionPeriods = pgTable(
    "subscription_periods",
    {
        id: uuid("id").primaryKey().defaultRandom(),
        userId: uuid("user_id")
            .notNull()
            .references(() => users.id),
        paymentId: uuid("payment_id")
            .notNull()
            .references(() => paymentReceipts.id),
        telegramChargeId: varchar("telegram_charge_id", {
            length: 255,
        }).notNull(),
        planId: varchar("plan_id", { length: 50 }).notNull(),
        credits: integer("credits").notNull(),
        startsAt: timestamp("starts_at", { withTimezone: true })
            .notNull()
            .defaultNow(),
        expiresAt: timestamp("expires_at", { withTimezone: true }).notNull(),
        status: varchar("status", { length: 20 }).notNull().default("active"), // active, refunded
        refundedAt: timestamp("refunded_at", { withTimezone: true }),
        meta: jsonb("meta").$type<Record<string, unknown>>(),
        createdAt: timestamp("created_at", { withTimezone: true })
            .notNull()
            .defaultNow(),
        updatedAt: timestamp("updated_at", { withTimezone: true })
            .notNull()
            .defaultNow()
            .$onUpdate(() => new Date()),
    },
    (t) => [
        uniqueIndex("subscription_periods_charge_unique").on(
            t.telegramChargeId,
        ),
        index("subscription_periods_user_status_expiry_idx").on(
            t.userId,
            t.status,
            t.expiresAt,
        ),
    ],
);

// ── Usage Quotas (daily free tier tracking) ──────────────────────────────────

export const usageQuotas = pgTable(
    "usage_quotas",
    {
        id: uuid("id").primaryKey().defaultRandom(),
        userId: uuid("user_id")
            .notNull()
            .references(() => users.id),
        chatId: uuid("chat_id")
            .notNull()
            .references(() => chats.id),
        usageDate: date("usage_date").notNull(),
        usage: jsonb("usage")
            .$type<Record<string, number>>()
            .notNull()
            .default({}), // e.g. { "imagine": 1, "webSearch": 3 }
        createdAt: timestamp("created_at", { withTimezone: true })
            .notNull()
            .defaultNow(),
        updatedAt: timestamp("updated_at", { withTimezone: true })
            .notNull()
            .defaultNow()
            .$onUpdate(() => new Date()),
    },
    (t) => [
        unique("usage_quotas_user_chat_date_unique").on(
            t.userId,
            t.chatId,
            t.usageDate,
        ),
        index("usage_quotas_user_id_idx").on(t.userId),
        index("usage_quotas_chat_id_idx").on(t.chatId),
        index("usage_quotas_date_idx").on(t.usageDate),
    ],
);

// ── Reminders ────────────────────────────────────────────────────────────────

export const reminders = pgTable(
    "reminders",
    {
        id: uuid("id").primaryKey().defaultRandom(),
        chatId: uuid("chat_id")
            .notNull()
            .references(() => chats.id),
        userId: uuid("user_id")
            .notNull()
            .references(() => users.id),
        description: text("description").notNull(), // human-readable description
        message: text("message"), // plain text to send (plain mode)
        prompt: text("prompt"), // LLM prompt to execute (LLM mode)
        usesLlm: boolean("uses_llm").notNull().default(false),
        fireAt: timestamp("fire_at", { withTimezone: true }), // one-time
        cronExpression: varchar("cron_expression", { length: 100 }), // recurring
        isRecurring: boolean("is_recurring").notNull().default(false),
        threadId: integer("thread_id"), // forum topic
        replyToMessageId: integer("reply_to_message_id"),
        status: varchar("status", { length: 20 }).notNull().default("active"), // active, completed, cancelled, failed
        lastFiredAt: timestamp("last_fired_at", { withTimezone: true }),
        fireCount: integer("fire_count").notNull().default(0),
        meta: jsonb("meta").$type<Record<string, unknown>>(),
        createdAt: timestamp("created_at", { withTimezone: true })
            .notNull()
            .defaultNow(),
        updatedAt: timestamp("updated_at", { withTimezone: true })
            .notNull()
            .defaultNow()
            .$onUpdate(() => new Date()),
    },
    (t) => [
        index("reminders_fire_at_idx").on(t.fireAt),
        index("reminders_chat_id_idx").on(t.chatId),
        index("reminders_user_id_idx").on(t.userId),
        index("reminders_status_idx").on(t.status),
        index("reminders_status_fire_at_idx").on(t.status, t.fireAt),
        index("reminders_chat_status_idx").on(t.chatId, t.status),
        index("reminders_user_status_recurring_idx").on(
            t.userId,
            t.status,
            t.isRecurring,
        ),
    ],
);

// ── Type exports for inference ───────────────────────────────────────────────

export type User = typeof users.$inferSelect;
export type NewUser = typeof users.$inferInsert;
export type Chat = typeof chats.$inferSelect;
export type NewChat = typeof chats.$inferInsert;
export type ChatMember = typeof chatMembers.$inferSelect;
export type Message = typeof messages.$inferSelect;
export type LedgerEntry = typeof ledger.$inferSelect;
export type PaymentReceipt = typeof paymentReceipts.$inferSelect;
export type ProviderCall = typeof providerCalls.$inferSelect;
export type CreditDebt = typeof creditDebts.$inferSelect;
export type CreditDebtEvent = typeof creditDebtEvents.$inferSelect;
export type QuotaWindow = typeof quotaWindows.$inferSelect;
export type PendingToolConfirmation =
    typeof pendingToolConfirmations.$inferSelect;
export type SubscriptionPeriod = typeof subscriptionPeriods.$inferSelect;
export type UsageQuota = typeof usageQuotas.$inferSelect;
export type Reminder = typeof reminders.$inferSelect;
