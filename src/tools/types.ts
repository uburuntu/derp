import type { z } from "zod";
import type { CreditService } from "../credits/service";
import type { CreditCheckResult } from "../credits/types";
import type { Database } from "../db/connection";
import type { ContextParticipant } from "../llm/context-builder";
import type { ModelCapability, ModelTier } from "../llm/registry";
import type {
    BinaryMedia,
    MediaAttachment,
    ProviderCallRecorder,
} from "../llm/types";

// ── Tool Categories ──────────────────────────────────────────────────────────

export type ToolCategory = "media" | "search" | "utility" | "reasoning";

// ── Tool Data Ports ─────────────────────────────────────────────────────────

export interface ToolMemoryStore {
    update(memory: string | null): Promise<void>;
}

export interface ToolReminderCreateInput {
    threadId?: number | null;
    description: string;
    message?: string | null;
    prompt?: string | null;
    usesLlm?: boolean;
    fireAt?: Date | null;
    cronExpression?: string | null;
    isRecurring?: boolean;
    replyToMessageId?: number | null;
}

export interface ToolReminderRecord {
    id: string;
    chatId: string;
    userId: string;
    threadId: number | null;
    description: string;
    fireAt: Date | null;
    cronExpression: string | null;
    isRecurring: boolean;
}

export interface ToolReminderStore {
    countActive(threadId?: number | null): Promise<number>;
    countRecurringForUser(): Promise<number>;
    create(input: ToolReminderCreateInput): Promise<void>;
    list(threadId?: number | null): Promise<ToolReminderRecord[]>;
    getById(id: string): Promise<ToolReminderRecord | null>;
    cancel(id: string): Promise<void>;
}

export interface ToolUser {
    id: string;
    telegramId: number;
    firstName?: string | null;
    username?: string | null;
}

export interface ToolChat {
    id: string;
    telegramId: number;
    memory?: string | null;
}

// ── Tool Definition ──────────────────────────────────────────────────────────

export interface ToolDefinition<TParams = unknown> {
    name: string;
    commands: string[]; // ['/imagine', '/i'] — first is primary
    description: string; // For LLM function calling
    helpText: string; // i18n key for /help
    category: ToolCategory;

    parameters: z.ZodSchema<TParams>;
    parseCommand?: (input: string, command?: string) => TParams;
    usage?: string;

    execute: (params: TParams, ctx: ToolContext) => Promise<ToolResult>;

    credits: number; // Flat cost (0 = free)
    freeDaily: number; // Daily free uses (0 = paid-only)

    capability?: ModelCapability;
    defaultModel?: string;

    minTier?: ModelTier;
    chatAdminOnly?: boolean; // Telegram chat admins, not bot admins
    allowAutoCall?: boolean; // Safe for model-initiated calls without explicit slash command
}

// ── Tool Context ─────────────────────────────────────────────────────────────

export interface ToolContext {
    user: ToolUser;
    chat: ToolChat;
    memoryStore: ToolMemoryStore;
    reminderStore: ToolReminderStore;
    providerRecorder: ProviderCallRecorder;
    tier: ModelTier;
    isChatAdmin: boolean;
    isGroupChat?: boolean;
    canManageMemory: boolean;
    canManageReminders: boolean;

    // Scoped participant references exposed to the LLM as p1, p2, ...
    participants?: Map<string, ContextParticipant>;
    getParticipantProfilePhoto?: (
        participantRef: string,
    ) => Promise<MediaAttachment | null>;

    // Telegram context helpers
    sendMessage: (text: string) => Promise<void>;
    sendPhoto: (photo: Buffer, caption?: string) => Promise<void>;
    sendVoice: (audio: Buffer) => Promise<void>;
    sendVideo: (video: Buffer, caption?: string) => Promise<void>;
    editMessage: (messageId: number, text: string) => Promise<void>;
    deleteMessage: (messageId: number) => Promise<void>;

    // Media from the triggering message
    replyMedia?: MediaAttachment[];
    threadId?: number | null;
    replyToMessageId?: number | null;
    idempotencyKey?: string;
    creditResult?: CreditCheckResult;
    expectedCreditSource?: CreditCheckResult["source"];
    expectedCreditsToDeduct?: number;
    recordProviderResult?: (result: {
        providerCallIds?: string[];
        costMicros?: number;
    }) => void;
}

export interface ToolExecutionContext extends ToolContext {
    db: Database;
    creditService: CreditService;
}

// ── Tool Result ──────────────────────────────────────────────────────────────

export interface ToolResult {
    /** Text response to include in the agent's output */
    text?: string;
    /** Image to send to chat */
    image?: BinaryMedia;
    /** Whether the tool already sent its own response (don't include in agent text) */
    handled?: boolean;
    /** Error message (tool failed but gracefully) */
    error?: string;
    /** Provider work succeeded, so paid credits should not be auto-refunded. */
    billableFailure?: boolean;
    /** Provider accounting rows created while producing this result. */
    providerCallIds?: string[];
    /** Provider-side cost in USD micros for this result. */
    costMicros?: number;
}
