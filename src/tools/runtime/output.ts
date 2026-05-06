import type { DerpContext } from "../../bot/context";
import { notifyAdmins } from "../../common/admin-notify";
import { logger } from "../../common/observability";
import {
    appendFooterToChunks,
    replyMarkdown,
    splitMessage,
} from "../../common/reply";
import { escapeHtml } from "../../common/sanitize";
import type { CreditCheckResult } from "../../credits/types";
import { markLedgerSpendStatus } from "../../db/queries/credits";
import { insertMessage } from "../../db/queries/messages";
import type { MessageMetadata } from "../../db/schema";
import type { ProviderResultMetadata } from "../../llm/provider-result";
import { isGroupChat } from "../../platform/telegram-access";
import type { ToolDefinition, ToolResult } from "../types";

export type ReplyOptions = Parameters<DerpContext["reply"]>[1];

export interface PersistableSentMessage {
    message_id: number;
    date: number;
    message_thread_id?: number;
    caption?: string;
    photo?: Array<{ file_id: string }>;
    voice?: { file_id: string };
    video?: { file_id: string };
}

interface OutgoingMessageRecord {
    contentType: string;
    text?: string | null;
    attachmentType?: string | null;
    attachmentFileId?: string | null;
    threadId?: number | null;
    replyToMessageId?: number | null;
    metadata?: MessageMetadata | null;
}

export function replyOptionsFor(
    threadId?: number | null,
    replyToMessageId?: number | null,
): ReplyOptions {
    return {
        message_thread_id: threadId ?? undefined,
        reply_to_message_id: replyToMessageId ?? undefined,
    };
}

export function publicCreditSource(
    ctx: DerpContext,
    source?: string,
): string | undefined {
    return source === "user" && isGroupChat(ctx) ? "user_private" : source;
}

export function largestPhotoFileId(
    sent: PersistableSentMessage,
): string | null | undefined {
    return sent.photo?.at(-1)?.file_id;
}

export async function persistOutgoingMessage(
    ctx: DerpContext,
    sent: PersistableSentMessage,
    record: OutgoingMessageRecord,
): Promise<void> {
    if (!ctx.dbChat) return;

    try {
        await insertMessage(ctx.db, {
            chatId: ctx.dbChat.id,
            userId: null,
            telegramMessageId: sent.message_id,
            threadId: sent.message_thread_id ?? record.threadId ?? null,
            direction: "out",
            contentType: record.contentType,
            text: record.text ?? null,
            attachmentType: record.attachmentType ?? null,
            attachmentFileId: record.attachmentFileId ?? null,
            replyToMessageId: record.replyToMessageId ?? null,
            metadata: record.metadata ?? null,
            telegramDate: new Date(sent.date * 1000),
        });
    } catch (err) {
        logger.warn("tool_command_output_persist_failed", {
            messageId: sent.message_id,
            error: err instanceof Error ? err.message : String(err),
        });
    }
}

export async function replyMarkdownAndPersist(
    ctx: DerpContext,
    chunks: string[],
    options: ReplyOptions,
    metadata: MessageMetadata,
    storedChunks: Array<string | null | undefined> = chunks,
): Promise<void> {
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
        const sentMessages = await replyMarkdown(ctx, chunk, chunkOptions);
        for (const [sentIndex, sent] of sentMessages.entries()) {
            await persistOutgoingMessage(ctx, sent, {
                contentType: "text",
                text: sentIndex === 0 ? (storedChunks[i] ?? null) : null,
                threadId: options?.message_thread_id ?? null,
                replyToMessageId:
                    sentIndex === 0
                        ? (chunkOptions?.reply_to_message_id ?? null)
                        : null,
                metadata,
            });
        }
    }
}

export function buildCommandMetadata(
    tool: ToolDefinition,
    ctx: DerpContext,
    startedAt: number,
    creditResult?: CreditCheckResult,
    providerMeta?: ProviderResultMetadata,
): MessageMetadata {
    return {
        model: creditResult?.modelId,
        tier: ctx.tier,
        toolsUsed: [tool.name],
        creditsSpent:
            creditResult && creditResult.creditsToDeduct > 0
                ? creditResult.creditsToDeduct
                : undefined,
        creditSource:
            creditResult && creditResult.source !== "rejected"
                ? creditResult.source
                : undefined,
        providerCallIds: providerMeta?.providerCallIds,
        costMicros: providerMeta?.costMicros,
        durationMs: Date.now() - startedAt,
    };
}

export async function sendToolResult(
    ctx: DerpContext,
    tool: ToolDefinition,
    commandStart: number,
    result: ToolResult & { creditResult?: CreditCheckResult },
    replyOptions: ReplyOptions,
): Promise<void> {
    if (result.handled || !result.text) return;

    const cost =
        result.creditResult &&
        result.creditResult.creditsToDeduct > 0 &&
        result.creditResult.creditsRemaining != null
            ? result.creditResult.creditsToDeduct
            : 0;
    const remaining = result.creditResult?.creditsRemaining ?? 0;
    const storedChunks = splitMessage(result.text);
    const chunksWithFooter = appendFooterToChunks(
        storedChunks,
        cost,
        remaining,
        publicCreditSource(ctx, result.creditResult?.source),
    );
    try {
        await replyMarkdownAndPersist(
            ctx,
            chunksWithFooter,
            replyOptions,
            buildCommandMetadata(tool, ctx, commandStart, result.creditResult, {
                providerCallIds: result.providerCallIds,
                costMicros: result.costMicros,
            }),
            storedChunks,
        );
        await markLedgerSpendStatus(
            ctx.db,
            result.creditResult?.ledgerId,
            "delivered",
            { toolName: tool.name },
        );
    } catch (err) {
        const error = err instanceof Error ? err.message : String(err);
        if (result.creditResult && result.creditResult.creditsToDeduct > 0) {
            await markLedgerSpendStatus(
                ctx.db,
                result.creditResult.ledgerId,
                "delivery_failed",
                {
                    error,
                    toolName: tool.name,
                    providerCallIds: result.providerCallIds,
                    costMicros: result.costMicros,
                },
            );
            logger.warn("tool_text_delivery_failed_after_spend", {
                tool: tool.name,
                error,
                creditsDeducted: result.creditResult.creditsToDeduct,
                source: result.creditResult.source,
                providerCallIds: result.providerCallIds,
                costMicros: result.costMicros,
            });
            await notifyAdmins(
                `⚠️ <b>Billable tool text delivery failure</b>\n\nTool: <code>${escapeHtml(tool.name)}</code>\nCredits kept: ${result.creditResult.creditsToDeduct}\nSource: <code>${escapeHtml(result.creditResult.source)}</code>\nProvider calls: <code>${escapeHtml(result.providerCallIds?.join(", ") ?? "n/a")}</code>\nProvider cost: $${((result.costMicros ?? 0) / 1_000_000).toFixed(4)}\nReason: ${escapeHtml(error)}`,
                { critical: true },
            ).catch((notifyErr) => {
                logger.error("tool_text_delivery_admin_notify_failed", {
                    tool: tool.name,
                    error:
                        notifyErr instanceof Error
                            ? notifyErr.message
                            : String(notifyErr),
                });
            });
        }
        throw err;
    }
}

export async function editCallbackMessageHtml(
    ctx: DerpContext,
    html: string,
): Promise<void> {
    try {
        await ctx.editMessageText(html, { parse_mode: "HTML" });
    } catch {
        await ctx.editMessageText(html.replace(/<[^>]*>/g, ""));
    }
}
