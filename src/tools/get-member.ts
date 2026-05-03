/** Get member tool — retrieves a chat member's profile photo for editing */

import { z } from "zod";
import type { ToolContext, ToolDefinition, ToolResult } from "./types";

const getMemberParamsSchema = z.object({
	participantRef: z
		.string()
		.regex(/^p\d+$/i)
		.describe(
			"Scoped participant reference from the PARTICIPANTS block, such as p1 or p2",
		),
});

type GetMemberParams = z.infer<typeof getMemberParamsSchema>;

async function executeGetMember(
	params: GetMemberParams,
	_ctx: ToolContext,
): Promise<ToolResult> {
	try {
		const participantRef = params.participantRef.trim().toLowerCase();
		const participant = _ctx.participants?.get(participantRef);
		if (!participant) {
			return {
				text: `Unknown participant reference: ${params.participantRef}`,
				error: "Unknown participant",
			};
		}

		const photo = await _ctx.getParticipantProfilePhoto?.(participantRef);
		if (!photo) {
			return {
				text: `${participant.firstName} has no accessible profile photo.`,
				error: "No profile photo",
			};
		}

		_ctx.replyMedia = [photo, ...(_ctx.replyMedia ?? [])];

		return {
			text: `Loaded ${participant.firstName}'s profile photo as the active image reference. Use editImage next to transform it.`,
		};
	} catch (err) {
		const msg = err instanceof Error ? err.message : String(err);
		return { text: `Failed to get member info: ${msg}`, error: msg };
	}
}

export const getMemberTool: ToolDefinition<GetMemberParams> = {
	name: "getMember",
	commands: [], // Agent-only
	description:
		"Load a chat member's profile photo into the current tool context for image editing. Use participantRef from the PARTICIPANTS block, never raw Telegram IDs.",
	helpText: "tool-get-member",
	category: "utility",
	parameters: getMemberParamsSchema,
	execute: executeGetMember,
	credits: 0,
	freeDaily: Number.POSITIVE_INFINITY,
	allowAutoCall: true,
};
