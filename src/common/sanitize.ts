/** Text sanitization utilities */

/** Escape text before interpolating it into Telegram HTML. */
export function escapeHtml(text: string): string {
	return text
		.replaceAll("&", "&amp;")
		.replaceAll("<", "&lt;")
		.replaceAll(">", "&gt;");
}

/** Strip MarkdownV2 formatting characters */
export function stripMarkdown(text: string): string {
	return text.replace(/\\([_*[\]()~`>#+\-=|{}.!])/g, "$1");
}

/** Truncate text to a maximum length, adding ellipsis if truncated */
export function truncate(text: string, maxLen: number): string {
	if (text.length <= maxLen) return text;
	return `${text.slice(0, maxLen - 1)}…`;
}

/** Remove zero-width characters and other invisible Unicode */
export function removeInvisible(text: string): string {
	return text.replace(/[\u200B-\u200D\uFEFF\u2060]/g, "");
}

/** Normalize whitespace (collapse multiple spaces/newlines) */
export function normalizeWhitespace(text: string): string {
	return text
		.replace(/[ \t]+/g, " ")
		.replace(/\n{3,}/g, "\n\n")
		.trim();
}
