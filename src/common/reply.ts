/** Shared reply module — message splitting, formatting, balance footer, media sending.
 *
 * Every handler and tool uses this module. Single implementation for all response delivery.
 */

const TELEGRAM_MSG_LIMIT = 4096;
const TELEGRAM_CAPTION_LIMIT = 1024;

/** Split a long message on paragraph → sentence → word boundaries */
export function splitMessage(
	text: string,
	maxLen = TELEGRAM_MSG_LIMIT,
): string[] {
	if (text.length <= maxLen) return [text];

	const chunks: string[] = [];
	let remaining = text;

	while (remaining.length > 0) {
		if (remaining.length <= maxLen) {
			chunks.push(remaining);
			break;
		}

		let splitAt = -1;

		// Try splitting at paragraph boundary
		const paragraphEnd = remaining.lastIndexOf("\n\n", maxLen);
		if (paragraphEnd > maxLen * 0.3) {
			splitAt = paragraphEnd + 2;
		}

		// Try splitting at newline
		if (splitAt === -1) {
			const newlineEnd = remaining.lastIndexOf("\n", maxLen);
			if (newlineEnd > maxLen * 0.3) {
				splitAt = newlineEnd + 1;
			}
		}

		// Try splitting at sentence boundary
		if (splitAt === -1) {
			const sentenceEnd = remaining.lastIndexOf(". ", maxLen);
			if (sentenceEnd > maxLen * 0.3) {
				splitAt = sentenceEnd + 2;
			}
		}

		// Try splitting at space
		if (splitAt === -1) {
			const spaceEnd = remaining.lastIndexOf(" ", maxLen);
			if (spaceEnd > maxLen * 0.3) {
				splitAt = spaceEnd + 1;
			}
		}

		// Hard split as last resort
		if (splitAt === -1) {
			splitAt = maxLen;
		}

		chunks.push(remaining.slice(0, splitAt));
		remaining = remaining.slice(splitAt);
	}

	return chunks;
}

/** Format a credit usage footer line */
export function formatBalanceFooter(cost: number, remaining: number): string {
	if (cost === 0) return "";
	if (remaining <= 20) {
		return `\n\n⚠️ ${cost} credits used · ${remaining} remaining · /buy to top up`;
	}
	return `\n\n✨ ${cost} credits used · ${remaining} remaining`;
}

/** Append balance footer to the last chunk of a split message */
export function appendFooterToChunks(
	chunks: string[],
	cost: number,
	remaining: number,
): string[] {
	if (cost === 0 || chunks.length === 0) return chunks;
	const footer = formatBalanceFooter(cost, remaining);
	const last = chunks[chunks.length - 1] + footer;

	// If appending footer exceeds limit, add as a new chunk
	if (last.length > TELEGRAM_MSG_LIMIT) {
		return [...chunks, footer.trim()];
	}

	return [...chunks.slice(0, -1), last];
}

/** Check if a caption needs overflow handling */
export function needsCaptionOverflow(caption: string): boolean {
	return caption.length > TELEGRAM_CAPTION_LIMIT;
}

/** Split caption into (short caption for media, full text as follow-up) */
export function splitCaption(caption: string): {
	mediaCaption: string;
	followUp: string;
} {
	if (caption.length <= TELEGRAM_CAPTION_LIMIT) {
		return { mediaCaption: caption, followUp: "" };
	}

	// Truncate caption at sentence boundary
	const truncAt = caption.lastIndexOf(". ", TELEGRAM_CAPTION_LIMIT - 3);
	if (truncAt > TELEGRAM_CAPTION_LIMIT * 0.3) {
		return {
			mediaCaption: `${caption.slice(0, truncAt + 1)}…`,
			followUp: caption,
		};
	}

	return {
		mediaCaption: `${caption.slice(0, TELEGRAM_CAPTION_LIMIT - 1)}…`,
		followUp: caption,
	};
}

/** Strip HTML tags for plain text fallback */
export function stripHtml(html: string): string {
	return html.replace(/<[^>]*>/g, "");
}
