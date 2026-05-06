/** Tool registry — auto-discovers tools, generates commands, LLM schemas, and help text */

import { type Bot, InlineKeyboard } from "grammy";
import { toJSONSchema, type z } from "zod";
import type { DerpContext } from "../bot/context";
import { notifyAdmins } from "../common/admin-notify";
import { downloadTelegramFile, extractMedia } from "../common/extractor";
import { logger } from "../common/observability";
import {
	appendFooterToChunks,
	replyHtml,
	replyMarkdown,
	splitMessage,
} from "../common/reply";
import { registerToolPricing } from "../credits/service";
import type { CreditCheckResult } from "../credits/types";
import { markLedgerSpendStatus } from "../db/queries/credits";
import {
	cancelPendingToolConfirmation,
	claimPendingToolConfirmation,
	createPendingToolConfirmation,
	getPendingToolConfirmation,
} from "../db/queries/finance";
import { insertMessage } from "../db/queries/messages";
import type { MessageMetadata } from "../db/schema";
import type { LLMToolSchema, MediaAttachment } from "../llm/types";
import { isToolDisabled } from "../preferences/user";
import { executeWithCreditGate } from "./credit-gate";
import type {
	ToolCategory,
	ToolContext,
	ToolDefinition,
	ToolResult,
} from "./types";

const CATEGORY_ORDER: ToolCategory[] = [
	"search",
	"reasoning",
	"media",
	"utility",
];

const CATEGORY_LABELS: Record<ToolCategory, string> = {
	search: "Search & Research",
	reasoning: "Reasoning",
	media: "Media",
	utility: "Utilities",
};

const CATEGORY_LABEL_KEYS: Record<ToolCategory, string> = {
	search: "tool-category-search",
	reasoning: "tool-category-reasoning",
	media: "tool-category-media",
	utility: "tool-category-utility",
};

const CATEGORY_EMOJI: Record<ToolCategory, string> = {
	search: "🔍",
	reasoning: "🧠",
	media: "🎨",
	utility: "🛠",
};

const TOOL_CONFIRM_COST_THRESHOLD = 20;
const TOOL_CONFIRM_TTL_MS = 5 * 60 * 1000;

type Translator = (
	key: string,
	args?: Record<string, string | number>,
) => string;
type ReplyOptions = Parameters<DerpContext["reply"]>[1];

interface ParsedToolParams {
	ok: true;
	params: unknown;
}

interface ToolUsageError {
	ok: false;
	usage: string;
}

type ToolParseResult = ParsedToolParams | ToolUsageError;

interface PendingMediaInfo {
	type: MediaAttachment["type"];
	fileId: string;
	mimeType: string;
}

interface ToolExecutionContextInput {
	commandStart: number;
	threadId?: number | null;
	replyToMessageId?: number | null;
	triggerReplyToMessageId?: number | null;
	replyMedia: MediaAttachment[];
	idempotencyKey?: string;
	expectedCreditSource?: CreditCheckResult["source"];
	expectedCreditsToDeduct?: number;
}

interface PersistableSentMessage {
	message_id: number;
	date: number;
	message_thread_id?: number;
	caption?: string;
	photo?: Array<{ file_id: string }>;
	voice?: { file_id: string };
	video?: { file_id: string };
}

interface OutgoingMessageRecord {
	contentType: string;
	text?: string | null;
	attachmentType?: string | null;
	attachmentFileId?: string | null;
	threadId?: number | null;
	replyToMessageId?: number | null;
	metadata?: MessageMetadata | null;
}

interface ProviderResultMetadata {
	providerCallIds?: string[];
	costMicros?: number;
}

function escapeHtml(text: string): string {
	return text
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;");
}

function callbackData(action: "run" | "cancel", id: string): string {
	return `tool_confirm:${action}:${id}`;
}

function replyOptionsFor(
	threadId?: number | null,
	replyToMessageId?: number | null,
): ReplyOptions {
	return {
		message_thread_id: threadId ?? undefined,
		reply_to_message_id: replyToMessageId ?? undefined,
	};
}

function primaryCommand(tool: ToolDefinition): string {
	return tool.commands[0] ?? tool.name;
}

function parseToolParams(
	tool: ToolDefinition,
	input: string,
	command?: string,
): ToolParseResult {
	const schemaShape = tool.parameters;

	try {
		const jsonSchema = zodToJsonSchema(schemaShape);
		const properties = (jsonSchema as Record<string, unknown>).properties as
			| Record<string, unknown>
			| undefined;
		const required = (jsonSchema as Record<string, unknown>).required as
			| string[]
			| undefined;

		let params: unknown;
		if (tool.parseCommand) {
			params = tool.parseCommand(input, command);
		} else if (properties && required && required.length > 0) {
			const firstField = required[0];
			params = firstField ? { [firstField]: input } : {};
		} else {
			params = { query: input };
		}

		const parsed = schemaShape.safeParse(params);
		if (!parsed.success) {
			const usage = `Usage: ${tool.usage ?? `${primaryCommand(tool)} <${required?.[0] ?? "input"}>`}`;
			return { ok: false, usage };
		}

		return { ok: true, params: parsed.data };
	} catch {
		return {
			ok: false,
			usage: `Usage: ${tool.usage ?? `${primaryCommand(tool)} <input>`}`,
		};
	}
}

function shouldConfirmToolSpend(result: CreditCheckResult): boolean {
	if (!result.allowed || result.creditsToDeduct <= 0) return false;
	return (
		result.source === "chat" ||
		result.creditsToDeduct >= TOOL_CONFIRM_COST_THRESHOLD
	);
}

function pendingMediaInfo(media: MediaAttachment[]): PendingMediaInfo[] {
	return media
		.filter((item): item is MediaAttachment & { fileId: string } =>
			Boolean(item.fileId),
		)
		.map((item) => ({
			type: item.type,
			fileId: item.fileId,
			mimeType: item.mimeType,
		}));
}

function readPendingMediaInfo(meta: unknown): PendingMediaInfo[] {
	if (!meta || typeof meta !== "object") return [];
	const value = (meta as { triggerMedia?: unknown }).triggerMedia;
	if (!Array.isArray(value)) return [];
	return value.filter((item): item is PendingMediaInfo => {
		if (!item || typeof item !== "object") return false;
		const media = item as PendingMediaInfo;
		return (
			["image", "video", "audio", "document"].includes(media.type) &&
			typeof media.fileId === "string" &&
			typeof media.mimeType === "string"
		);
	});
}

function readTriggerReplyToMessageId(meta: unknown): number | null {
	if (!meta || typeof meta !== "object") return null;
	const value = (meta as { triggerReplyToMessageId?: unknown })
		.triggerReplyToMessageId;
	return typeof value === "number" ? value : null;
}

async function downloadPendingMedia(
	ctx: DerpContext,
	infos: PendingMediaInfo[],
): Promise<MediaAttachment[]> {
	const media: MediaAttachment[] = [];
	for (const info of infos) {
		media.push({
			type: info.type,
			data: await downloadTelegramFile(ctx.api, info.fileId),
			mimeType: info.mimeType,
			fileId: info.fileId,
		});
	}
	return media;
}

function formatToolCost(tool: ToolDefinition, t?: Translator): string {
	const hasFiniteQuota = Number.isFinite(tool.freeDaily) && tool.freeDaily > 0;
	if (tool.credits === 0) {
		if (!hasFiniteQuota) return t ? t("tool-cost-free") : "free";
		return t
			? t("tool-cost-free-daily", { freeDaily: tool.freeDaily })
			: `${tool.freeDaily} free per user/chat/day`;
	}

	if (!hasFiniteQuota) {
		return t
			? t("tool-cost-credits", { credits: tool.credits })
			: `${tool.credits} cr`;
	}

	return t
		? t("tool-cost-credits-with-quota", {
				credits: tool.credits,
				freeDaily: tool.freeDaily,
			})
		: `${tool.credits} cr, ${tool.freeDaily} free per user/chat/day`;
}

async function isChatAdmin(ctx: DerpContext): Promise<boolean> {
	if (ctx.chat?.type === "private") return true;
	if (!ctx.from) return false;

	try {
		const member = await ctx.getChatMember(ctx.from.id);
		return member.status === "administrator" || member.status === "creator";
	} catch {
		return false;
	}
}

function canUseAdminGatedSetting(
	setting: "admins" | "everyone" | undefined,
	isAdmin: boolean,
): boolean {
	return setting !== "admins" || isAdmin;
}

async function extractTriggerMedia(
	ctx: DerpContext,
): Promise<MediaAttachment[]> {
	const attachments: MediaAttachment[] = [];
	if (ctx.message) {
		for (const media of await extractMedia(ctx.api, ctx.message)) {
			attachments.push(media);
		}
	}
	if (ctx.message?.reply_to_message) {
		for (const media of await extractMedia(
			ctx.api,
			ctx.message.reply_to_message,
		)) {
			attachments.push(media);
		}
	}
	return attachments;
}

async function persistOutgoingMessage(
	ctx: DerpContext,
	sent: PersistableSentMessage,
	record: OutgoingMessageRecord,
): Promise<void> {
	if (!ctx.dbChat) return;

	try {
		await insertMessage(ctx.db, {
			chatId: ctx.dbChat.id,
			userId: null,
			telegramMessageId: sent.message_id,
			threadId: sent.message_thread_id ?? record.threadId ?? null,
			direction: "out",
			contentType: record.contentType,
			text: record.text ?? null,
			attachmentType: record.attachmentType ?? null,
			attachmentFileId: record.attachmentFileId ?? null,
			replyToMessageId: record.replyToMessageId ?? null,
			metadata: record.metadata ?? null,
			telegramDate: new Date(sent.date * 1000),
		});
	} catch (err) {
		logger.warn("tool_command_output_persist_failed", {
			messageId: sent.message_id,
			error: err instanceof Error ? err.message : String(err),
		});
	}
}

async function replyMarkdownAndPersist(
	ctx: DerpContext,
	chunks: string[],
	options: ReplyOptions,
	metadata: MessageMetadata,
	storedChunks: Array<string | null | undefined> = chunks,
): Promise<void> {
	for (let i = 0; i < chunks.length; i++) {
		const chunk = chunks[i];
		if (!chunk) continue;
		const chunkOptions =
			i === 0
				? options
				: {
						...options,
						reply_to_message_id: undefined,
					};
		const sentMessages = await replyMarkdown(ctx, chunk, chunkOptions);
		for (const [sentIndex, sent] of sentMessages.entries()) {
			await persistOutgoingMessage(ctx, sent, {
				contentType: "text",
				text: sentIndex === 0 ? (storedChunks[i] ?? null) : null,
				threadId: options?.message_thread_id ?? null,
				replyToMessageId:
					sentIndex === 0 ? (chunkOptions?.reply_to_message_id ?? null) : null,
				metadata,
			});
		}
	}
}

function buildCommandMetadata(
	tool: ToolDefinition,
	ctx: DerpContext,
	startedAt: number,
	creditResult?: Awaited<
		ReturnType<typeof executeWithCreditGate>
	>["creditResult"],
	providerMeta?: ProviderResultMetadata,
): MessageMetadata {
	return {
		model: creditResult?.modelId,
		tier: ctx.tier,
		toolsUsed: [tool.name],
		creditsSpent:
			creditResult && creditResult.creditsToDeduct > 0
				? creditResult.creditsToDeduct
				: undefined,
		creditSource:
			creditResult && creditResult.source !== "rejected"
				? creditResult.source
				: undefined,
		providerCallIds: providerMeta?.providerCallIds,
		costMicros: providerMeta?.costMicros,
		durationMs: Date.now() - startedAt,
	};
}

function mergeProviderResultMetadata(
	target: ProviderResultMetadata,
	result: ProviderResultMetadata,
): void {
	if (result.providerCallIds?.length) {
		const ids = new Set([
			...(target.providerCallIds ?? []),
			...result.providerCallIds,
		]);
		target.providerCallIds = [...ids];
	}
	if (result.costMicros && result.costMicros > 0) {
		target.costMicros = (target.costMicros ?? 0) + result.costMicros;
	}
}

function hasProviderResultMetadata(meta: ProviderResultMetadata): boolean {
	return Boolean(meta.providerCallIds?.length || (meta.costMicros ?? 0) > 0);
}

function isGroupChat(ctx: DerpContext): boolean {
	return ctx.chat?.type === "group" || ctx.chat?.type === "supergroup";
}

function publicCreditSource(
	ctx: DerpContext,
	source?: string,
): string | undefined {
	return source === "user" && isGroupChat(ctx) ? "user_private" : source;
}

function largestPhotoFileId(
	sent: PersistableSentMessage,
): string | null | undefined {
	return sent.photo?.at(-1)?.file_id;
}

async function buildToolContext(
	ctx: DerpContext,
	tool: ToolDefinition,
	input: ToolExecutionContextInput,
): Promise<ToolContext> {
	if (!ctx.dbUser || !ctx.dbChat || !ctx.creditService) {
		throw new Error("Missing hydrated tool context");
	}

	const admin = await isChatAdmin(ctx);
	const replyOptions = replyOptionsFor(input.threadId, input.replyToMessageId);
	const providerMeta: ProviderResultMetadata = {};

	const toolCtx: ToolContext = {
		db: ctx.db,
		user: ctx.dbUser,
		chat: ctx.dbChat,
		creditService: ctx.creditService,
		tier: ctx.tier,
		isChatAdmin: admin,
		isGroupChat: isGroupChat(ctx),
		canManageMemory: canUseAdminGatedSetting(
			ctx.dbChat.settings?.memoryAccess,
			admin,
		),
		canManageReminders: canUseAdminGatedSetting(
			ctx.dbChat.settings?.remindersAccess,
			admin,
		),
		recordProviderResult: (result) =>
			mergeProviderResultMetadata(providerMeta, result),
		sendMessage: async (text: string) => {
			const sent = await ctx.reply(text, replyOptions);
			await persistOutgoingMessage(ctx, sent, {
				contentType: "text",
				text,
				threadId: input.threadId ?? null,
				replyToMessageId: input.replyToMessageId ?? null,
				metadata: buildCommandMetadata(
					tool,
					ctx,
					input.commandStart,
					hasProviderResultMetadata(providerMeta)
						? toolCtx.creditResult
						: undefined,
					providerMeta,
				),
			});
		},
		sendPhoto: async (photo: Buffer, caption?: string) => {
			const { InputFile } = await import("grammy");
			const sent = await ctx.replyWithPhoto(new InputFile(photo), {
				caption,
				...replyOptions,
			});
			await persistOutgoingMessage(ctx, sent, {
				contentType: "photo",
				text: caption ?? sent.caption ?? null,
				attachmentType: "image",
				attachmentFileId: largestPhotoFileId(sent) ?? null,
				threadId: input.threadId ?? null,
				replyToMessageId: input.replyToMessageId ?? null,
				metadata: buildCommandMetadata(
					tool,
					ctx,
					input.commandStart,
					toolCtx.creditResult,
					providerMeta,
				),
			});
		},
		sendVoice: async (audio: Buffer) => {
			const { InputFile } = await import("grammy");
			const sent = await ctx.replyWithVoice(new InputFile(audio), replyOptions);
			await persistOutgoingMessage(ctx, sent, {
				contentType: "voice",
				attachmentType: "voice",
				attachmentFileId: sent.voice?.file_id ?? null,
				threadId: input.threadId ?? null,
				replyToMessageId: input.replyToMessageId ?? null,
				metadata: buildCommandMetadata(
					tool,
					ctx,
					input.commandStart,
					toolCtx.creditResult,
					providerMeta,
				),
			});
		},
		sendVideo: async (video: Buffer, caption?: string) => {
			const { InputFile } = await import("grammy");
			const sent = await ctx.replyWithVideo(new InputFile(video), {
				caption,
				...replyOptions,
			});
			await persistOutgoingMessage(ctx, sent, {
				contentType: "video",
				text: caption ?? sent.caption ?? null,
				attachmentType: "video",
				attachmentFileId: sent.video?.file_id ?? null,
				threadId: input.threadId ?? null,
				replyToMessageId: input.replyToMessageId ?? null,
				metadata: buildCommandMetadata(
					tool,
					ctx,
					input.commandStart,
					toolCtx.creditResult,
					providerMeta,
				),
			});
		},
		editMessage: async (messageId: number, text: string) => {
			const chatId = ctx.chat?.id;
			if (chatId == null) throw new Error("No chat for editMessage");
			await ctx.api.editMessageText(chatId, messageId, text);
		},
		deleteMessage: async (messageId: number) => {
			const chatId = ctx.chat?.id;
			if (chatId == null) throw new Error("No chat for deleteMessage");
			await ctx.api.deleteMessage(chatId, messageId);
		},
		replyMedia: input.replyMedia,
		threadId: input.threadId ?? null,
		replyToMessageId: input.triggerReplyToMessageId ?? null,
		idempotencyKey: input.idempotencyKey,
		expectedCreditSource: input.expectedCreditSource,
		expectedCreditsToDeduct: input.expectedCreditsToDeduct,
	};
	return toolCtx;
}

async function sendToolResult(
	ctx: DerpContext,
	tool: ToolDefinition,
	commandStart: number,
	result: ToolResult & { creditResult?: CreditCheckResult },
	replyOptions: ReplyOptions,
): Promise<void> {
	if (result.handled || !result.text) return;

	const cost =
		result.creditResult &&
		result.creditResult.creditsToDeduct > 0 &&
		result.creditResult.creditsRemaining != null
			? result.creditResult.creditsToDeduct
			: 0;
	const remaining = result.creditResult?.creditsRemaining ?? 0;
	const storedChunks = splitMessage(result.text);
	const chunksWithFooter = appendFooterToChunks(
		storedChunks,
		cost,
		remaining,
		publicCreditSource(ctx, result.creditResult?.source),
	);
	try {
		await replyMarkdownAndPersist(
			ctx,
			chunksWithFooter,
			replyOptions,
			buildCommandMetadata(tool, ctx, commandStart, result.creditResult, {
				providerCallIds: result.providerCallIds,
				costMicros: result.costMicros,
			}),
			storedChunks,
		);
		await markLedgerSpendStatus(
			ctx.db,
			result.creditResult?.ledgerId,
			"delivered",
			{ toolName: tool.name },
		);
	} catch (err) {
		const error = err instanceof Error ? err.message : String(err);
		if (result.creditResult && result.creditResult.creditsToDeduct > 0) {
			await markLedgerSpendStatus(
				ctx.db,
				result.creditResult.ledgerId,
				"delivery_failed",
				{
					error,
					toolName: tool.name,
					providerCallIds: result.providerCallIds,
					costMicros: result.costMicros,
				},
			);
			logger.warn("tool_text_delivery_failed_after_spend", {
				tool: tool.name,
				error,
				creditsDeducted: result.creditResult.creditsToDeduct,
				source: result.creditResult.source,
				providerCallIds: result.providerCallIds,
				costMicros: result.costMicros,
			});
			await notifyAdmins(
				`⚠️ <b>Billable tool text delivery failure</b>\n\nTool: <code>${escapeHtml(tool.name)}</code>\nCredits kept: ${result.creditResult.creditsToDeduct}\nSource: <code>${escapeHtml(result.creditResult.source)}</code>\nProvider calls: <code>${escapeHtml(result.providerCallIds?.join(", ") ?? "n/a")}</code>\nProvider cost: $${((result.costMicros ?? 0) / 1_000_000).toFixed(4)}\nReason: ${escapeHtml(error)}`,
				{ critical: true },
			).catch((notifyErr) => {
				logger.error("tool_text_delivery_admin_notify_failed", {
					tool: tool.name,
					error:
						notifyErr instanceof Error ? notifyErr.message : String(notifyErr),
				});
			});
		}
		throw err;
	}
}

async function editCallbackMessageHtml(
	ctx: DerpContext,
	html: string,
): Promise<void> {
	try {
		await ctx.editMessageText(html, { parse_mode: "HTML" });
	} catch {
		await ctx.editMessageText(html.replace(/<[^>]*>/g, ""));
	}
}

class ToolRegistry {
	private tools = new Map<string, ToolDefinition>();
	private commandMap = new Map<string, ToolDefinition>();

	/** Register a tool definition */
	// biome-ignore lint: Tool params are validated at runtime via Zod
	register(tool: ToolDefinition<any>): void {
		if (this.tools.has(tool.name)) {
			throw new Error(`Duplicate tool name: ${tool.name}`);
		}

		this.tools.set(tool.name, tool);

		// Map all commands to this tool
		for (const cmd of tool.commands) {
			const name = cmd.replace(/^\//, "");
			const existing = this.commandMap.get(name);
			if (existing) {
				throw new Error(
					`Duplicate command ${cmd}: already registered by ${existing.name}`,
				);
			}
			this.commandMap.set(name, tool);
		}

		// Register pricing with CreditService
		registerToolPricing(tool.name, {
			credits: tool.credits,
			freeDaily: tool.freeDaily,
			capability: tool.capability,
			defaultModel: tool.defaultModel,
		});
	}

	/** Get a tool by name */
	getTool(name: string): ToolDefinition | undefined {
		return this.tools.get(name);
	}

	/** Get a tool by command (e.g., "search" or "s") */
	getToolByCommand(command: string): ToolDefinition | undefined {
		return this.commandMap.get(command);
	}

	/** Get all registered tools */
	getTools(): ToolDefinition[] {
		return [...this.tools.values()];
	}

	/** Generate LLM function-calling schemas for all tools */
	getLLMToolSchemas(disabledTools: Iterable<string> = []): LLMToolSchema[] {
		const schemas: LLMToolSchema[] = [];
		const disabled = new Set(disabledTools);

		for (const tool of this.tools.values()) {
			if (disabled.has(tool.name)) continue;

			// Convert Zod schema to JSON Schema for the LLM
			const zodSchema = tool.parameters;
			const jsonSchema = zodToJsonSchema(zodSchema);

			schemas.push({
				name: tool.name,
				description: tool.description,
				parameters: jsonSchema,
			});
		}

		return schemas;
	}

	/** Generate LLM function schemas for tools safe to call without explicit command intent. */
	getAutoCallableLLMToolSchemas(
		disabledTools: Iterable<string> = [],
	): LLMToolSchema[] {
		const schemas: LLMToolSchema[] = [];
		const disabled = new Set(disabledTools);

		for (const tool of this.tools.values()) {
			if (!tool.allowAutoCall) continue;
			if (disabled.has(tool.name)) continue;
			schemas.push({
				name: tool.name,
				description: tool.description,
				parameters: zodToJsonSchema(tool.parameters),
			});
		}

		return schemas;
	}

	/** Generate help text grouped by category (Telegram HTML) */
	getHelpText(t?: Translator): string {
		const grouped = new Map<ToolCategory, ToolDefinition[]>();

		for (const tool of this.tools.values()) {
			const list = grouped.get(tool.category) ?? [];
			list.push(tool);
			grouped.set(tool.category, list);
		}

		const sections: string[] = [];

		for (const category of CATEGORY_ORDER) {
			const tools = grouped.get(category);
			if (!tools || tools.length === 0) continue;

			const emoji = CATEGORY_EMOJI[category];
			const label = escapeHtml(
				t ? t(CATEGORY_LABEL_KEYS[category]) : CATEGORY_LABELS[category],
			);
			const lines = tools
				.map((tool) => {
					if (tool.commands.length === 0) return null; // skip agent-only tools
					const cmds = tool.commands.join(", ");
					const description = escapeHtml(
						t ? t(tool.helpText) : tool.description,
					);
					const cost = escapeHtml(formatToolCost(tool, t));
					return `  ${cmds} — ${description} · <i>${cost}</i>`;
				})
				.filter(Boolean);

			if (lines.length === 0) continue;
			sections.push(`${emoji} <b>${label}</b>\n${lines.join("\n")}`);
		}

		return sections.join("\n\n");
	}

	/** Get BotCommand array for setMyCommands */
	getBotCommands(): Array<{ command: string; description: string }> {
		const commands: Array<{ command: string; description: string }> = [];

		for (const tool of this.tools.values()) {
			const primary = tool.commands[0];
			if (!primary) continue;
			commands.push({
				command: primary.replace(/^\//, ""),
				description: tool.description,
			});
		}

		return commands;
	}

	/** Auto-generate grammY command handlers for all tools with commands */
	registerCommandHandlers(bot: Bot<DerpContext>): void {
		for (const tool of this.tools.values()) {
			if (tool.commands.length === 0) continue;

			const commandNames = tool.commands.map((c) => c.replace(/^\//, ""));

			bot.command(commandNames, async (ctx) => {
				if (!ctx.dbUser || !ctx.dbChat || !ctx.creditService) return;

				const commandStart = Date.now();
				if (isToolDisabled(ctx.dbUser.preferences, tool.name)) {
					const primaryCmd = tool.commands[0] ?? tool.name;
					const text = ctx.t("tool-disabled", { tool: primaryCmd });
					await replyMarkdownAndPersist(
						ctx,
						[text],
						{
							message_thread_id: ctx.message?.message_thread_id,
							reply_to_message_id: ctx.message?.message_id,
						},
						buildCommandMetadata(tool, ctx, commandStart),
						[text],
					);
					return;
				}

				const input = ctx.match ?? "";
				const command = ctx.message?.text
					?.match(/^\/([^\s@]+)/)?.[1]
					?.toLowerCase();
				const parsed = parseToolParams(tool, input, command);
				const threadId = ctx.message?.message_thread_id ?? null;
				const replyToMessageId = ctx.message?.message_id ?? null;
				const triggerReplyToMessageId =
					ctx.message?.reply_to_message?.message_id ?? null;
				const replyOptions = replyOptionsFor(threadId, replyToMessageId);

				if (!parsed.ok) {
					await replyMarkdownAndPersist(
						ctx,
						[parsed.usage],
						replyOptions,
						buildCommandMetadata(tool, ctx, commandStart),
						[parsed.usage],
					);
					return;
				}

				const media = await extractTriggerMedia(ctx);
				const idempotencyKey =
					ctx.chat && ctx.message
						? `tool:${tool.name}:cmd:${ctx.chat.id}:${ctx.message.message_id}`
						: undefined;
				const preflight = await ctx.creditService.checkToolAccess(tool.name);
				if (shouldConfirmToolSpend(preflight)) {
					await this.requestToolConfirmation(
						ctx,
						tool,
						parsed.params,
						preflight,
						{
							idempotencyKey,
							media,
							replyOptions,
							threadId,
							replyToMessageId,
							triggerReplyToMessageId,
						},
					);
					return;
				}

				const toolCtx = await buildToolContext(ctx, tool, {
					commandStart,
					threadId,
					replyToMessageId,
					triggerReplyToMessageId,
					replyMedia: media,
					idempotencyKey,
				});
				const result = await executeWithCreditGate(
					tool,
					parsed.params,
					toolCtx,
				);
				await sendToolResult(ctx, tool, commandStart, result, replyOptions);
			});
		}

		this.registerConfirmationHandlers(bot);
	}

	private async requestToolConfirmation(
		ctx: DerpContext,
		tool: ToolDefinition,
		params: unknown,
		creditResult: CreditCheckResult,
		options: {
			idempotencyKey?: string;
			media: MediaAttachment[];
			replyOptions: ReplyOptions;
			threadId?: number | null;
			replyToMessageId?: number | null;
			triggerReplyToMessageId?: number | null;
		},
	): Promise<void> {
		if (!ctx.dbUser || !ctx.dbChat || !ctx.chat) return;

		const pendingSource = creditResult.source === "chat" ? "chat" : "user";
		const idempotencyKey =
			options.idempotencyKey ??
			`tool:${tool.name}:pending:${ctx.chat.id}:${ctx.from?.id ?? "unknown"}:${Date.now()}`;
		const id = await createPendingToolConfirmation(ctx.db, {
			userId: ctx.dbUser.id,
			chatId: ctx.dbChat.id,
			telegramChatId: ctx.chat.id,
			messageId: options.replyToMessageId ?? null,
			threadId: options.threadId ?? null,
			toolName: tool.name,
			params: params as Record<string, unknown>,
			cost: creditResult.creditsToDeduct,
			source: pendingSource,
			idempotencyKey,
			expiresAt: new Date(Date.now() + TOOL_CONFIRM_TTL_MS),
			meta: {
				triggerMedia: pendingMediaInfo(options.media),
				triggerReplyToMessageId: options.triggerReplyToMessageId ?? null,
			},
		});

		const source =
			pendingSource === "chat"
				? ctx.t("tool-confirm-source-chat")
				: ctx.t("tool-confirm-source-user");
		const keyboard = new InlineKeyboard()
			.text(ctx.t("tool-confirm-run"), callbackData("run", id))
			.text(ctx.t("tool-confirm-cancel"), callbackData("cancel", id));

		await replyHtml(
			ctx,
			ctx.t(
				isGroupChat(ctx) && pendingSource === "user"
					? "tool-confirm-message-private"
					: "tool-confirm-message",
				{
					tool: primaryCommand(tool),
					cost: creditResult.creditsToDeduct,
					source,
					remaining: creditResult.creditsRemaining ?? 0,
				},
			),
			{ ...options.replyOptions, reply_markup: keyboard },
		);
	}

	private registerConfirmationHandlers(bot: Bot<DerpContext>): void {
		bot.callbackQuery(/^tool_confirm:run:([0-9a-f-]+)$/i, async (ctx) => {
			const id = ctx.match[1];
			if (!id || !ctx.dbUser || !ctx.dbChat || !ctx.creditService) {
				await ctx.answerCallbackQuery(ctx.t("error-generic"));
				return;
			}

			const existing = await getPendingToolConfirmation(ctx.db, id);
			if (existing && existing.userId !== ctx.dbUser.id) {
				await ctx.answerCallbackQuery(ctx.t("tool-confirm-owner-only"));
				return;
			}

			const pending = await claimPendingToolConfirmation(ctx.db, {
				id,
				userId: ctx.dbUser.id,
			});
			if (!pending || pending.chatId !== ctx.dbChat.id) {
				await ctx.answerCallbackQuery(ctx.t("tool-confirm-expired"));
				await editCallbackMessageHtml(ctx, ctx.t("tool-confirm-expired"));
				return;
			}

			const tool = this.getTool(pending.toolName);
			if (!tool) {
				await ctx.answerCallbackQuery(ctx.t("error-generic"));
				await editCallbackMessageHtml(ctx, ctx.t("tool-confirm-invalid"));
				return;
			}

			const parsed = tool.parameters.safeParse(pending.params);
			if (!parsed.success) {
				await ctx.answerCallbackQuery(ctx.t("error-generic"));
				await editCallbackMessageHtml(ctx, ctx.t("tool-confirm-invalid"));
				return;
			}

			const commandStart = Date.now();
			const replyOptions = replyOptionsFor(pending.threadId, pending.messageId);
			await ctx.answerCallbackQuery(
				ctx.t("tool-confirm-running-alert", { tool: primaryCommand(tool) }),
			);
			await editCallbackMessageHtml(
				ctx,
				ctx.t("tool-confirm-running", { tool: primaryCommand(tool) }),
			);

			let media: MediaAttachment[];
			try {
				media = await downloadPendingMedia(
					ctx,
					readPendingMediaInfo(pending.meta),
				);
			} catch (err) {
				logger.warn("tool_confirmation_media_download_failed", {
					tool: tool.name,
					error: err instanceof Error ? err.message : String(err),
				});
				await editCallbackMessageHtml(ctx, ctx.t("tool-confirm-failed"));
				return;
			}

			const toolCtx = await buildToolContext(ctx, tool, {
				commandStart,
				threadId: pending.threadId,
				replyToMessageId: pending.messageId,
				triggerReplyToMessageId: readTriggerReplyToMessageId(pending.meta),
				replyMedia: media,
				idempotencyKey: pending.idempotencyKey,
				expectedCreditSource:
					pending.source === "chat" || pending.source === "user"
						? pending.source
						: undefined,
				expectedCreditsToDeduct: pending.cost,
			});
			const result = await executeWithCreditGate(tool, parsed.data, toolCtx);
			await sendToolResult(ctx, tool, commandStart, result, replyOptions);
			await editCallbackMessageHtml(
				ctx,
				ctx.t(
					result.error && result.billableFailure
						? "tool-confirm-billable-failed"
						: result.error
							? "tool-confirm-failed"
							: "tool-confirm-done",
					{
						tool: primaryCommand(tool),
					},
				),
			);
		});

		bot.callbackQuery(/^tool_confirm:cancel:([0-9a-f-]+)$/i, async (ctx) => {
			const id = ctx.match[1];
			if (!id || !ctx.dbUser) {
				await ctx.answerCallbackQuery(ctx.t("error-generic"));
				return;
			}

			const existing = await getPendingToolConfirmation(ctx.db, id);
			if (existing && existing.userId !== ctx.dbUser.id) {
				await ctx.answerCallbackQuery(ctx.t("tool-confirm-owner-only"));
				return;
			}

			const cancelled = await cancelPendingToolConfirmation(ctx.db, {
				id,
				userId: ctx.dbUser.id,
			});
			const key = cancelled ? "tool-confirm-cancelled" : "tool-confirm-expired";
			await ctx.answerCallbackQuery(ctx.t(key));
			await editCallbackMessageHtml(ctx, ctx.t(key));
		});
	}
}

/** Convert a Zod schema to a JSON Schema object for Gemini function calling */
function zodToJsonSchema(schema: z.ZodSchema): Record<string, unknown> {
	return toJSONSchema(schema) as Record<string, unknown>;
}

/** Singleton registry */
export const toolRegistry = new ToolRegistry();
