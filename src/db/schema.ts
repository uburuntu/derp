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
	uuid,
	varchar,
} from "drizzle-orm/pg-core";

// ── Users ────────────────────────────────────────────────────────────────────

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
		telegramDate: timestamp("telegram_date", { withTimezone: true }).notNull(),
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
	creditSource?: string; // "user" | "chat" | "free"
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
		type: varchar("type", { length: 20 }).notNull(), // purchase, spend, refund, grant, transfer
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
		usage: jsonb("usage").$type<Record<string, number>>().notNull().default({}), // e.g. { "imagine": 1, "webSearch": 3 }
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
export type UsageQuota = typeof usageQuotas.$inferSelect;
export type Reminder = typeof reminders.$inferSelect;
