/** Shared reply module — message splitting, formatting, balance footer, media sending.
 *
 * Every handler and tool uses this module. Single implementation for all response delivery.
 */

import { markdownToHtml, stripHtmlTags } from "./markdown";

const TELEGRAM_MSG_LIMIT = 4096;
const TELEGRAM_CAPTION_LIMIT = 1024;

export interface ReplyOptions {
    parse_mode?: string;
    message_thread_id?: number;
    reply_to_message_id?: number;
    [key: string]: unknown;
}

interface ReplyTarget<TSent> {
    reply(text: string, options?: ReplyOptions): Promise<TSent>;
}

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
export function formatBalanceFooter(
    cost: number,
    remaining: number,
    source?: string,
): string {
    if (cost === 0) return "";
    const sourceText =
        source === "chat"
            ? " from group pool"
            : source === "user"
              ? " from personal balance"
              : source === "user_private"
                ? " from personal balance"
                : "";
    if (source === "user_private") {
        return `\n\n✨ ${cost} credits used${sourceText}`;
    }
    if (remaining <= 20) {
        return `\n\n⚠️ ${cost} credits used${sourceText} · ${remaining} remaining · /buy to top up`;
    }
    return `\n\n✨ ${cost} credits used${sourceText} · ${remaining} remaining`;
}

/** Append balance footer to the last chunk of a split message */
export function appendFooterToChunks(
    chunks: string[],
    cost: number,
    remaining: number,
    source?: string,
): string[] {
    if (cost === 0 || chunks.length === 0) return chunks;
    const footer = formatBalanceFooter(cost, remaining, source);
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

/** Split caption into a short media caption and remaining follow-up text */
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
            followUp: caption.slice(truncAt + 2).trimStart(),
        };
    }

    return {
        mediaCaption: `${caption.slice(0, TELEGRAM_CAPTION_LIMIT - 1)}…`,
        followUp: caption.slice(TELEGRAM_CAPTION_LIMIT - 1),
    };
}

/** Prepare a Telegram-safe caption plus follow-up message chunks */
export function captionPartsForMedia(caption?: string): {
    caption?: string;
    followUpChunks: string[];
} {
    if (!caption) return { followUpChunks: [] };
    const { mediaCaption, followUp } = splitCaption(caption);
    return {
        caption: mediaCaption || undefined,
        followUpChunks: followUp ? splitMessage(followUp) : [],
    };
}

/** Strip HTML tags for plain text fallback */
export function stripHtml(html: string): string {
    return html.replace(/<[^>]*>/g, "");
}

/** Reply with Telegram HTML and fall back to plain text if entity parsing fails. */
export async function replyHtml<TSent>(
    ctx: ReplyTarget<TSent>,
    html: string,
    options: ReplyOptions = {},
) {
    try {
        return await ctx.reply(html, { ...options, parse_mode: "HTML" });
    } catch {
        const { parse_mode: _parseMode, ...plainOptions } = options ?? {};
        return await ctx.reply(stripHtmlTags(html), plainOptions);
    }
}

/** Reply with standard Markdown converted to Telegram HTML, split into safe chunks. */
export async function replyMarkdown<TSent>(
    ctx: ReplyTarget<TSent>,
    text: string,
    options: ReplyOptions = {},
) {
    const sent = [];
    const chunks = splitMessage(text);
    for (let i = 0; i < chunks.length; i++) {
        const chunk = chunks[i];
        if (!chunk) continue;
        const chunkOptions =
            i === 0
                ? options
                : {
                      ...options,
                      reply_to_message_id: undefined,
                  };
        sent.push(await replyHtml(ctx, markdownToHtml(chunk), chunkOptions));
    }
    return sent;
}
