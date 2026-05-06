import { InputFile } from "grammy";
import type { DerpContext } from "../../bot/context";
import type { CreditCheckResult } from "../../credits/types";
import {
    hasProviderResultMetadata,
    mergeProviderResultMetadata,
    type ProviderResultMetadata,
} from "../../llm/provider-result";
import type { MediaAttachment } from "../../llm/types";
import { createDbProviderCallRecorder } from "../../platform/provider-call-recorder";
import {
    canUseAdminGatedSetting,
    isChatAdmin,
    isGroupChat,
} from "../../platform/telegram-access";
import {
    createDbToolMemoryStore,
    createDbToolReminderStore,
} from "../../platform/tool-stores";
import type { ToolDefinition, ToolExecutionContext } from "../types";
import {
    buildCommandMetadata,
    largestPhotoFileId,
    persistOutgoingMessage,
    replyOptionsFor,
} from "./output";

export interface ToolExecutionContextInput {
    commandStart: number;
    threadId?: number | null;
    replyToMessageId?: number | null;
    triggerReplyToMessageId?: number | null;
    replyMedia: MediaAttachment[];
    idempotencyKey?: string;
    expectedCreditSource?: CreditCheckResult["source"];
    expectedCreditsToDeduct?: number;
}

export async function createToolExecutionContext(
    ctx: DerpContext,
    tool: ToolDefinition,
    input: ToolExecutionContextInput,
): Promise<ToolExecutionContext> {
    if (!ctx.dbUser || !ctx.dbChat || !ctx.creditService) {
        throw new Error("Missing hydrated tool context");
    }

    const admin = await isChatAdmin(ctx);
    const replyOptions = replyOptionsFor(
        input.threadId,
        input.replyToMessageId,
    );
    const providerMeta: ProviderResultMetadata = {};

    const toolCtx: ToolExecutionContext = {
        db: ctx.db,
        user: ctx.dbUser,
        chat: ctx.dbChat,
        creditService: ctx.creditService,
        memoryStore: createDbToolMemoryStore(ctx.db, ctx.dbChat),
        reminderStore: createDbToolReminderStore(
            ctx.db,
            ctx.dbUser,
            ctx.dbChat,
        ),
        providerRecorder: createDbProviderCallRecorder(ctx.db),
        tier: ctx.tier,
        isChatAdmin: admin,
        isGroupChat: isGroupChat(ctx),
        canManageMemory: canUseAdminGatedSetting(
            ctx.dbChat.settings?.memoryAccess,
            admin,
        ),
        canManageReminders: canUseAdminGatedSetting(
            ctx.dbChat.settings?.remindersAccess,
            admin,
        ),
        recordProviderResult: (result) =>
            mergeProviderResultMetadata(providerMeta, result),
        sendMessage: async (text: string) => {
            const sent = await ctx.reply(text, replyOptions);
            await persistOutgoingMessage(ctx, sent, {
                contentType: "text",
                text,
                threadId: input.threadId ?? null,
                replyToMessageId: input.replyToMessageId ?? null,
                metadata: buildCommandMetadata(
                    tool,
                    ctx,
                    input.commandStart,
                    hasProviderResultMetadata(providerMeta)
                        ? toolCtx.creditResult
                        : undefined,
                    providerMeta,
                ),
            });
        },
        sendPhoto: async (photo: Buffer, caption?: string) => {
            const sent = await ctx.replyWithPhoto(new InputFile(photo), {
                caption,
                ...replyOptions,
            });
            await persistOutgoingMessage(ctx, sent, {
                contentType: "photo",
                text: caption ?? sent.caption ?? null,
                attachmentType: "image",
                attachmentFileId: largestPhotoFileId(sent) ?? null,
                threadId: input.threadId ?? null,
                replyToMessageId: input.replyToMessageId ?? null,
                metadata: buildCommandMetadata(
                    tool,
                    ctx,
                    input.commandStart,
                    toolCtx.creditResult,
                    providerMeta,
                ),
            });
        },
        sendVoice: async (audio: Buffer) => {
            const sent = await ctx.replyWithVoice(
                new InputFile(audio),
                replyOptions,
            );
            await persistOutgoingMessage(ctx, sent, {
                contentType: "voice",
                attachmentType: "voice",
                attachmentFileId: sent.voice?.file_id ?? null,
                threadId: input.threadId ?? null,
                replyToMessageId: input.replyToMessageId ?? null,
                metadata: buildCommandMetadata(
                    tool,
                    ctx,
                    input.commandStart,
                    toolCtx.creditResult,
                    providerMeta,
                ),
            });
        },
        sendVideo: async (video: Buffer, caption?: string) => {
            const sent = await ctx.replyWithVideo(new InputFile(video), {
                caption,
                ...replyOptions,
            });
            await persistOutgoingMessage(ctx, sent, {
                contentType: "video",
                text: caption ?? sent.caption ?? null,
                attachmentType: "video",
                attachmentFileId: sent.video?.file_id ?? null,
                threadId: input.threadId ?? null,
                replyToMessageId: input.replyToMessageId ?? null,
                metadata: buildCommandMetadata(
                    tool,
                    ctx,
                    input.commandStart,
                    toolCtx.creditResult,
                    providerMeta,
                ),
            });
        },
        editMessage: async (messageId: number, text: string) => {
            const chatId = ctx.chat?.id;
            if (chatId == null) throw new Error("No chat for editMessage");
            await ctx.api.editMessageText(chatId, messageId, text);
        },
        deleteMessage: async (messageId: number) => {
            const chatId = ctx.chat?.id;
            if (chatId == null) throw new Error("No chat for deleteMessage");
            await ctx.api.deleteMessage(chatId, messageId);
        },
        replyMedia: input.replyMedia,
        threadId: input.threadId ?? null,
        replyToMessageId: input.triggerReplyToMessageId ?? null,
        idempotencyKey: input.idempotencyKey,
        expectedCreditSource: input.expectedCreditSource,
        expectedCreditsToDeduct: input.expectedCreditsToDeduct,
    };
    return toolCtx;
}
