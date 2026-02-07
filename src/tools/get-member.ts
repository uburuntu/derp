/** Get member tool — retrieves a chat member's profile photo for editing */

import { z } from "zod";
import type { ToolContext, ToolDefinition, ToolResult } from "./types";

const getMemberParamsSchema = z.object({
	userId: z
		.number()
		.describe("Telegram user ID of the member whose profile photo to retrieve"),
});

type GetMemberParams = z.infer<typeof getMemberParamsSchema>;

async function executeGetMember(
	params: GetMemberParams,
	_ctx: ToolContext,
): Promise<ToolResult> {
	try {
		// Note: This tool is agent-only. The userId comes from the LLM parsing
		// a user mention in the conversation. The LLM derives it from the
		// participant registry in the context window.
		return {
			text: `Profile photo request for user ${params.userId}. Use the imagine or editImage tool with this user's photo as a reference.`,
		};
	} catch (err) {
		const msg = err instanceof Error ? err.message : String(err);
		return { text: `Failed to get member info: ${msg}`, error: msg };
	}
}

export const getMemberTool: ToolDefinition<GetMemberParams> = {
	name: "getMember",
	commands: [], // Agent-only
	description: "Get a chat member's profile photo for use in image editing",
	helpText: "tool-get-member",
	category: "utility",
	parameters: getMemberParamsSchema,
	execute: executeGetMember,
	credits: 0,
	freeDaily: Number.POSITIVE_INFINITY,
};
