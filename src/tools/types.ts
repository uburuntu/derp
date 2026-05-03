import type { z } from "zod";
import type { CreditService } from "../credits/service";
import type { Database } from "../db/connection";
import type { Chat, User } from "../db/schema";
import type { ContextParticipant } from "../llm/context-builder";
import type { ModelCapability, ModelTier } from "../llm/registry";
import type { BinaryMedia, MediaAttachment } from "../llm/types";

// ── Tool Categories ──────────────────────────────────────────────────────────

export type ToolCategory = "media" | "search" | "utility" | "reasoning";

// ── Tool Definition ──────────────────────────────────────────────────────────

export interface ToolDefinition<TParams = unknown> {
	name: string;
	commands: string[]; // ['/imagine', '/i'] — first is primary
	description: string; // For LLM function calling
	helpText: string; // i18n key for /help
	category: ToolCategory;

	parameters: z.ZodSchema<TParams>;
	parseCommand?: (input: string) => TParams;
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
	db: Database;
	user: User;
	chat: Chat;
	creditService: CreditService;
	tier: ModelTier;
	isChatAdmin: boolean;
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
}
