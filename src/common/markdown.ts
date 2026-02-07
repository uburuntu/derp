/** Convert standard Markdown to Telegram-safe HTML.
 *
 * Pipeline: protect code blocks → escape HTML entities → convert formatting → restore code blocks.
 * Telegram HTML only supports: <b>, <i>, <u>, <s>, <code>, <pre>, <a>, <blockquote>, <tg-spoiler>.
 * Unknown tags are shown as plain text, so this is safe even if conversion is imperfect.
 */

/** Escape HTML entities in text (the only 3 chars Telegram HTML cares about) */
function escapeHtml(text: string): string {
	return text
		.replace(/&/g, "&amp;")
		.replace(/</g, "&lt;")
		.replace(/>/g, "&gt;");
}

/** Strip all HTML tags for plain-text fallback */
export function stripHtmlTags(html: string): string {
	return html.replace(/<[^>]*>/g, "");
}

export function markdownToHtml(md: string): string {
	// Step 1: Extract and protect fenced code blocks (```...```)
	const codeBlocks: string[] = [];
	let text = md.replace(/```(\w*)\n?([\s\S]*?)```/g, (_match, lang, code) => {
		const escaped = escapeHtml(code.trimEnd());
		const langAttr = lang ? ` class="language-${escapeHtml(lang)}"` : "";
		const placeholder = `\x00CB${codeBlocks.length}\x00`;
		codeBlocks.push(`<pre><code${langAttr}>${escaped}</code></pre>`);
		return placeholder;
	});

	// Step 2: Extract and protect inline code (`...`)
	const inlineCodes: string[] = [];
	text = text.replace(/`([^`\n]+)`/g, (_match, code) => {
		const placeholder = `\x00IC${inlineCodes.length}\x00`;
		inlineCodes.push(`<code>${escapeHtml(code)}</code>`);
		return placeholder;
	});

	// Step 3: Escape HTML entities in remaining text
	text = text.replace(/[^]*?/g, (segment) => {
		// Don't escape our placeholders
		if (segment.startsWith("\x00")) return segment;
		return segment;
	});
	// Actually escape the whole thing, but our placeholders don't contain <>&
	text = escapeHtml(text);

	// Step 4: Convert Markdown formatting to HTML
	// Bold: **text** (must come before italic to handle ***bold italic***)
	text = text.replace(/\*\*\*(.+?)\*\*\*/g, "<b><i>$1</i></b>");
	text = text.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");

	// Italic: *text* (but not inside words like file*name)
	text = text.replace(/(?<!\w)\*([^\s*](?:.*?[^\s*])?)\*(?!\w)/g, "<i>$1</i>");

	// Strikethrough: ~~text~~
	text = text.replace(/~~(.+?)~~/g, "<s>$1</s>");

	// Links: [text](url)
	text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>');

	// Blockquotes: > text (at start of line)
	text = text.replace(/^&gt; (.+)$/gm, "<blockquote>$1</blockquote>");
	// Merge adjacent blockquotes
	text = text.replace(/<\/blockquote>\n<blockquote>/g, "\n");

	// Step 5: Restore code blocks and inline code
	for (let i = 0; i < codeBlocks.length; i++) {
		text = text.replace(`\x00CB${i}\x00`, codeBlocks[i]!);
	}
	for (let i = 0; i < inlineCodes.length; i++) {
		text = text.replace(`\x00IC${i}\x00`, inlineCodes[i]!);
	}

	return text;
}
