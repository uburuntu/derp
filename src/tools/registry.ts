/** Tool registry — auto-discovers tools, generates commands, LLM schemas, and help text */

import type { Bot } from "grammy";
import { toJSONSchema, type z } from "zod";
import type { DerpContext } from "../bot/context";
import { extractMedia } from "../common/extractor";
import { formatBalanceFooter, replyMarkdown } from "../common/reply";
import { registerToolPricing } from "../credits/service";
import type { LLMToolSchema, MediaAttachment } from "../llm/types";
import { executeWithCreditGate } from "./credit-gate";
import type { ToolCategory, ToolContext, ToolDefinition } from "./types";

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

type Translator = (
	key: string,
	args?: Record<string, string | number>,
) => string;

function escapeHtml(text: string): string {
	return text
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;");
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
	getLLMToolSchemas(): LLMToolSchema[] {
		const schemas: LLMToolSchema[] = [];

		for (const tool of this.tools.values()) {
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
	getAutoCallableLLMToolSchemas(): LLMToolSchema[] {
		const schemas: LLMToolSchema[] = [];

		for (const tool of this.tools.values()) {
			if (!tool.allowAutoCall) continue;
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

				const input = ctx.match ?? "";

				// Build the primary parameter from ctx.match
				// Most tools have a single required string param (query, prompt, text, etc.)
				const schemaShape = tool.parameters;
				let params: unknown;

				try {
					// Try parsing the input as the first required field
					const jsonSchema = zodToJsonSchema(schemaShape);
					const properties = (jsonSchema as Record<string, unknown>)
						.properties as Record<string, unknown> | undefined;
					const required = (jsonSchema as Record<string, unknown>).required as
						| string[]
						| undefined;

					if (tool.parseCommand) {
						params = tool.parseCommand(input);
					} else if (properties && required && required.length > 0) {
						const firstField = required[0];
						if (!firstField) {
							params = {};
						} else {
							params = { [firstField]: input };
						}
					} else {
						params = { query: input };
					}

					const parsed = schemaShape.safeParse(params);
					if (!parsed.success) {
						const primaryCmd = tool.commands[0] ?? tool.name;
						await replyMarkdown(
							ctx,
							`Usage: ${tool.usage ?? `${primaryCmd} <${required?.[0] ?? "input"}>`}`,
							{ reply_to_message_id: ctx.message?.message_id },
						);
						return;
					}

					params = parsed.data;
				} catch {
					const primaryCmd = tool.commands[0] ?? tool.name;
					await replyMarkdown(
						ctx,
						`Usage: ${tool.usage ?? `${primaryCmd} <input>`}`,
						{ reply_to_message_id: ctx.message?.message_id },
					);
					return;
				}

				const admin = await isChatAdmin(ctx);
				const media = await extractTriggerMedia(ctx);
				const toolCtx: ToolContext = {
					db: ctx.db,
					user: ctx.dbUser,
					chat: ctx.dbChat,
					creditService: ctx.creditService,
					tier: ctx.tier,
					isChatAdmin: admin,
					canManageMemory: canUseAdminGatedSetting(
						ctx.dbChat.settings?.memoryAccess,
						admin,
					),
					canManageReminders: canUseAdminGatedSetting(
						ctx.dbChat.settings?.remindersAccess,
						admin,
					),
					sendMessage: async (text: string) => {
						await ctx.reply(text);
					},
					sendPhoto: async (photo: Buffer, caption?: string) => {
						const { InputFile } = await import("grammy");
						await ctx.replyWithPhoto(new InputFile(photo), { caption });
					},
					sendVoice: async (audio: Buffer) => {
						const { InputFile } = await import("grammy");
						await ctx.replyWithVoice(new InputFile(audio));
					},
					sendVideo: async (video: Buffer, caption?: string) => {
						const { InputFile } = await import("grammy");
						await ctx.replyWithVideo(new InputFile(video), { caption });
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
					replyMedia: media,
					threadId: ctx.message?.message_thread_id ?? null,
					replyToMessageId: ctx.message?.message_id ?? null,
					idempotencyKey:
						ctx.chat && ctx.message
							? `tool:${tool.name}:cmd:${ctx.chat.id}:${ctx.message.message_id}`
							: undefined,
				};

				const result = await executeWithCreditGate(tool, params, toolCtx);

				if (!result.handled && result.text) {
					const footer =
						result.creditResult &&
						result.creditResult.creditsToDeduct > 0 &&
						result.creditResult.creditsRemaining != null
							? formatBalanceFooter(
									result.creditResult.creditsToDeduct,
									result.creditResult.creditsRemaining,
								)
							: "";
					await replyMarkdown(ctx, `${result.text}${footer}`, {
						reply_to_message_id: ctx.message?.message_id,
					});
				}
			});
		}
	}
}

/** Convert a Zod schema to a JSON Schema object for Gemini function calling */
function zodToJsonSchema(schema: z.ZodSchema): Record<string, unknown> {
	return toJSONSchema(schema) as Record<string, unknown>;
}

/** Singleton registry */
export const toolRegistry = new ToolRegistry();
