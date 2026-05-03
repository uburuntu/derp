/** Compact context window builder — participant registry + message stream.
 *
 * Optimized for Gemini's automatic context caching:
 * - System prompt + PARTICIPANTS block = stable prefix (rarely changes)
 * - MESSAGES block = dynamic suffix (grows with each message)
 */

import type { Message } from "../db/schema";

export interface ContextParticipant {
	userId: string;
	telegramId: number;
	firstName: string;
	lastName: string | null;
	username: string | null;
	role: string;
}

export interface BuiltContext {
	participants: string;
	messageStream: string;
	participantRefs: Map<string, ContextParticipant>;
}

/** Build compact context from messages and member data */
export function buildContext(
	msgs: Message[],
	members: Map<string, ContextParticipant>,
	botUsername: string,
): BuiltContext {
	// Collect participant IDs that appear in messages
	const activeUserIds = new Set<string>();
	for (const msg of msgs) {
		if (msg.userId) activeUserIds.add(msg.userId);
	}

	// Build participant registry (only users in the current message batch)
	const participantLines: string[] = [];
	const participantRefs = new Map<string, ContextParticipant>();
	let nextParticipantNumber = 1;
	for (const userId of activeUserIds) {
		const member = members.get(userId);
		if (!member) continue;

		const participantRef = `p${nextParticipantNumber++}`;
		participantRefs.set(participantRef, member);

		const name = member.lastName
			? `${member.firstName} ${member.lastName}`
			: member.firstName;
		const handle = member.username ? ` (@${member.username})` : "";
		const roleTag = member.role !== "member" ? ` [${member.role}]` : "";
		participantLines.push(`- ${participantRef}: ${name}${handle}${roleTag}`);
	}

	// Add bot as participant
	participantLines.push(`- Derp (bot, @${botUsername}) [bot]`);

	const participants = `# PARTICIPANTS\n${participantLines.join("\n")}`;

	// Build message stream
	const messageLines: string[] = [];
	for (const msg of msgs) {
		const member = msg.userId ? members.get(msg.userId) : null;
		const senderName =
			msg.direction === "out" ? "Derp" : (member?.firstName ?? "Unknown");

		const replyRef =
			msg.replyToMessageId != null ? ` (→${msg.replyToMessageId})` : "";

		// Build content
		const parts: string[] = [];

		// Media tags
		if (msg.attachmentType) {
			const fileRef = msg.attachmentFileId
				? ` file_id:${msg.attachmentFileId}`
				: "";
			parts.push(`[${msg.attachmentType}${fileRef}]`);
		}

		// Text content
		if (msg.text) {
			parts.push(msg.text);
		}

		const content = parts.join(" ") || "[empty]";
		messageLines.push(
			`[${msg.telegramMessageId}] ${senderName}${replyRef}: ${content}`,
		);
	}

	const messageStream = `# MESSAGES\n${messageLines.join("\n")}`;

	return { participants, messageStream, participantRefs };
}
