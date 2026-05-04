/** Chat memory tool — per-chat persistent memory for the LLM agent */

import { z } from "zod";
import { updateChatMemory } from "../db/queries/chats";
import {
	addChatMemoryItem,
	type ChatMemoryKind,
	formatChatMemoryForDisplay,
} from "../memory/structured";
import type { ToolContext, ToolDefinition, ToolResult } from "./types";

const memoryParamsSchema = z.object({
	action: z
		.enum(["read", "update", "clear"])
		.describe(
			"Action: read the current memory, update it with new content, or clear it",
		),
	kind: z
		.enum(["fact", "preference", "topic"])
		.optional()
		.describe("Type of memory item to add when action is update"),
	content: z
		.string()
		.optional()
		.describe("New memory content (required for update action)"),
});

type MemoryParams = z.infer<typeof memoryParamsSchema>;

function parseKindPrefix(input: string): {
	kind: ChatMemoryKind;
	content: string;
} {
	const match = input.match(/^(fact|preference|topic)\s*:\s*(.+)$/i);
	if (!match?.[1] || !match[2]) return { kind: "fact", content: input };
	return {
		kind: match[1].toLowerCase() as ChatMemoryKind,
		content: match[2],
	};
}

function parseMemoryCommand(input: string, command?: string): MemoryParams {
	const normalized = command?.replace(/^\//, "");
	if (normalized === "memory_clear" || normalized === "clear_memory") {
		return { action: "clear" };
	}
	if (normalized === "memory_set" || normalized === "set_memory") {
		const parsed = parseKindPrefix(input.trim());
		return { action: "update", ...parsed };
	}
	return { action: "read" };
}

async function executeMemory(
	params: MemoryParams,
	ctx: ToolContext,
): Promise<ToolResult> {
	switch (params.action) {
		case "read": {
			return {
				text: `Current chat memory:\n${formatChatMemoryForDisplay(ctx.chat.memory)}`,
			};
		}

		case "update": {
			if (!params.content) {
				return {
					text: "No content provided for memory update.",
					error: "Missing content",
				};
			}

			if (!ctx.canManageMemory) {
				return {
					text: "Only chat admins can update memory in this chat.",
					error: "Unauthorized",
				};
			}

			const kind = params.kind ?? "fact";
			const memory = addChatMemoryItem(ctx.chat.memory, kind, params.content);
			await updateChatMemory(ctx.db, ctx.chat.id, memory);
			ctx.chat.memory = memory;
			return { text: `Memory updated (${kind}).` };
		}

		case "clear": {
			if (!ctx.canManageMemory) {
				return {
					text: "Only chat admins can clear memory in this chat.",
					error: "Unauthorized",
				};
			}
			await updateChatMemory(ctx.db, ctx.chat.id, null);
			ctx.chat.memory = null;
			return { text: "Chat memory cleared." };
		}

		default:
			return { text: "Unknown memory action.", error: "Unknown action" };
	}
}

export const memoryTool: ToolDefinition<MemoryParams> = {
	name: "memory",
	commands: [
		"/memory",
		"/memory_set",
		"/set_memory",
		"/memory_clear",
		"/clear_memory",
	],
	description:
		"Read, update, or clear the persistent chat memory. Use this to remember important facts about users and the chat.",
	helpText: "tool-memory",
	category: "utility",
	parameters: memoryParamsSchema,
	parseCommand: parseMemoryCommand,
	usage:
		"/memory | /memory_set [fact|preference|topic]: <text> | /memory_clear",
	execute: executeMemory,
	credits: 0,
	freeDaily: Number.POSITIVE_INFINITY,
};
