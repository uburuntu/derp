/** Tool registry — auto-discovers tools, generates commands, LLM schemas, and help text */

import type { Bot } from "grammy";
import { toJSONSchema, type z } from "zod";
import { registerToolPricing } from "../credits/service";
import type { LLMToolSchema } from "../llm/types";
import { executeWithCreditGate } from "./credit-gate";
import type { ToolCategory, ToolDefinition } from "./types";

const CATEGORY_ORDER: ToolCategory[] = [
	"search",
	"reasoning",
	"media",
	"utility",
];

const CATEGORY_LABELS: Record<ToolCategory, string> = {
	search: "Search & Research",
	reasoning: "Reasoning",
	media: "Creative",
	utility: "Utilities",
};

const CATEGORY_EMOJI: Record<ToolCategory, string> = {
	search: "🔍",
	reasoning: "🧠",
	media: "🎨",
	utility: "🛠",
};

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
			if (this.commandMap.has(name)) {
				throw new Error(
					`Duplicate command ${cmd}: already registered by ${this.commandMap.get(name)!.name}`,
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

	/** Generate help text grouped by category (Telegram HTML) */
	getHelpText(): string {
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
			const label = CATEGORY_LABELS[category];
			const lines = tools
				.map((t) => {
					if (t.commands.length === 0) return null; // skip agent-only tools
					const cmds = t.commands.join(", ");
					const cost =
						t.credits === 0
							? "free"
							: `${t.credits} cr${t.freeDaily > 0 ? `, ${t.freeDaily} free/day` : ""}`;
					return `  ${cmds} — ${t.description} · <i>${cost}</i>`;
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
	// biome-ignore lint: Bot type uses any context
	registerCommandHandlers(bot: Bot<any>): void {
		for (const tool of this.tools.values()) {
			if (tool.commands.length === 0) continue;

			const commandNames = tool.commands.map((c) => c.replace(/^\//, ""));

			bot.command(commandNames, async (ctx: any) => {
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

					if (properties && required && required.length > 0) {
						const firstField = required[0]!;
						params = { [firstField]: input };
					} else {
						params = { query: input };
					}

					const parsed = schemaShape.safeParse(params);
					if (!parsed.success) {
						const primaryCmd = tool.commands[0] ?? tool.name;
						await ctx.reply(
							`Usage: ${primaryCmd} <${required?.[0] ?? "input"}>`,
						);
						return;
					}

					params = parsed.data;
				} catch {
					params = {};
				}

				const toolCtx = {
					db: ctx.db,
					user: ctx.dbUser,
					chat: ctx.dbChat,
					creditService: ctx.creditService,
					tier: ctx.tier,
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
						await ctx.api.editMessageText(ctx.chat!.id, messageId, text);
					},
					deleteMessage: async (messageId: number) => {
						await ctx.api.deleteMessage(ctx.chat!.id, messageId);
					},
				};

				const result = await executeWithCreditGate(tool, params, toolCtx);

				if (!result.handled && result.text) {
					await ctx.reply(result.text, {
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
