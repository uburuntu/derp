/** Chat memory tool — per-chat persistent memory for the LLM agent */

import { z } from "zod";
import { updateChatMemory } from "../db/queries/chats";
import type { ToolContext, ToolDefinition, ToolResult } from "./types";

const MAX_MEMORY_LENGTH = 4096;

const memoryParamsSchema = z.object({
	action: z
		.enum(["read", "update", "clear"])
		.describe(
			"Action: read the current memory, update it with new content, or clear it",
		),
	content: z
		.string()
		.optional()
		.describe("New memory content (required for update action)"),
});

type MemoryParams = z.infer<typeof memoryParamsSchema>;

async function executeMemory(
	params: MemoryParams,
	ctx: ToolContext,
): Promise<ToolResult> {
	switch (params.action) {
		case "read": {
			const memory = ctx.chat.memory;
			if (!memory) {
				return { text: "No memory stored for this chat yet." };
			}
			return { text: `Current chat memory:\n${memory}` };
		}

		case "update": {
			if (!params.content) {
				return {
					text: "No content provided for memory update.",
					error: "Missing content",
				};
			}

			// Enforce max length
			const content = params.content.slice(0, MAX_MEMORY_LENGTH);

			// Append to existing memory or replace
			const existing = ctx.chat.memory;
			const newMemory = existing
				? `${existing}\n${content}`.slice(0, MAX_MEMORY_LENGTH)
				: content;

			await updateChatMemory(ctx.db, ctx.chat.id, newMemory);
			return { text: "Memory updated." };
		}

		case "clear": {
			await updateChatMemory(ctx.db, ctx.chat.id, null);
			return { text: "Chat memory cleared." };
		}

		default:
			return { text: "Unknown memory action.", error: "Unknown action" };
	}
}

export const memoryTool: ToolDefinition<MemoryParams> = {
	name: "memory",
	commands: [], // Agent-only, no commands
	description:
		"Read, update, or clear the persistent chat memory. Use this to remember important facts about users and the chat.",
	helpText: "tool-memory",
	category: "utility",
	parameters: memoryParamsSchema,
	execute: executeMemory,
	credits: 0,
	freeDaily: Number.POSITIVE_INFINITY,
};
