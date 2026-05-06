/** Tool registry — auto-discovers tools, generates commands, LLM schemas, and help text */

import { type Bot, InlineKeyboard } from "grammy";
import type { DerpContext } from "../bot/context";
import { downloadTelegramFile, extractMedia } from "../common/extractor";
import { logger } from "../common/observability";
import { replyHtml } from "../common/reply";
import { registerToolPricing } from "../credits/service";
import type { CreditCheckResult } from "../credits/types";
import {
    cancelPendingToolConfirmation,
    claimPendingToolConfirmation,
    createPendingToolConfirmation,
    getPendingToolConfirmation,
} from "../db/queries/finance";
import type { LLMToolSchema, MediaAttachment } from "../llm/types";
import { isGroupChat } from "../platform/telegram-access";
import { isToolDisabled } from "../preferences/user";
import {
    CATEGORY_EMOJI,
    CATEGORY_LABEL_KEYS,
    CATEGORY_LABELS,
    CATEGORY_ORDER,
    formatToolCost,
    primaryCommand,
    type Translator,
} from "./catalog";
import { executeWithCreditGate } from "./credit-gate";
import { createToolExecutionContext } from "./runtime/context";
import {
    buildCommandMetadata,
    editCallbackMessageHtml,
    type ReplyOptions,
    replyMarkdownAndPersist,
    replyOptionsFor,
    sendToolResult,
} from "./runtime/output";
import { parseToolParams, zodToJsonSchema } from "./schema";
import type { ToolCategory, ToolDefinition } from "./types";

const TOOL_CONFIRM_COST_THRESHOLD = 20;
const TOOL_CONFIRM_TTL_MS = 5 * 60 * 1000;

interface PendingMediaInfo {
    type: MediaAttachment["type"];
    fileId: string;
    mimeType: string;
}

function escapeHtml(text: string): string {
    return text
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
}

function callbackData(action: "run" | "cancel", id: string): string {
    return `tool_confirm:${action}:${id}`;
}

function shouldConfirmToolSpend(result: CreditCheckResult): boolean {
    if (!result.allowed || result.creditsToDeduct <= 0) return false;
    return (
        result.source === "chat" ||
        result.creditsToDeduct >= TOOL_CONFIRM_COST_THRESHOLD
    );
}

function pendingMediaInfo(media: MediaAttachment[]): PendingMediaInfo[] {
    return media
        .filter((item): item is MediaAttachment & { fileId: string } =>
            Boolean(item.fileId),
        )
        .map((item) => ({
            type: item.type,
            fileId: item.fileId,
            mimeType: item.mimeType,
        }));
}

function readPendingMediaInfo(meta: unknown): PendingMediaInfo[] {
    if (!meta || typeof meta !== "object") return [];
    const value = (meta as { triggerMedia?: unknown }).triggerMedia;
    if (!Array.isArray(value)) return [];
    return value.filter((item): item is PendingMediaInfo => {
        if (!item || typeof item !== "object") return false;
        const media = item as PendingMediaInfo;
        return (
            ["image", "video", "audio", "document"].includes(media.type) &&
            typeof media.fileId === "string" &&
            typeof media.mimeType === "string"
        );
    });
}

function readTriggerReplyToMessageId(meta: unknown): number | null {
    if (!meta || typeof meta !== "object") return null;
    const value = (meta as { triggerReplyToMessageId?: unknown })
        .triggerReplyToMessageId;
    return typeof value === "number" ? value : null;
}

async function downloadPendingMedia(
    ctx: DerpContext,
    infos: PendingMediaInfo[],
): Promise<MediaAttachment[]> {
    const media: MediaAttachment[] = [];
    for (const info of infos) {
        media.push({
            type: info.type,
            data: await downloadTelegramFile(ctx.api, info.fileId),
            mimeType: info.mimeType,
            fileId: info.fileId,
        });
    }
    return media;
}

async function extractTriggerMedia(
    ctx: DerpContext,
): Promise<MediaAttachment[]> {
    const attachments: MediaAttachment[] = [];
    if (ctx.message) {
        for (const media of await extractMedia(ctx.api, ctx.message)) {
            attachments.push(media);
        }
    }
    if (ctx.message?.reply_to_message) {
        for (const media of await extractMedia(
            ctx.api,
            ctx.message.reply_to_message,
        )) {
            attachments.push(media);
        }
    }
    return attachments;
}

class ToolRegistry {
    private tools = new Map<string, ToolDefinition>();
    private commandMap = new Map<string, ToolDefinition>();

    /** Register a tool definition */
    // biome-ignore lint: Tool params are validated at runtime via Zod
    register(tool: ToolDefinition<any>): void {
        if (this.tools.has(tool.name)) {
            throw new Error(`Duplicate tool name: ${tool.name}`);
        }

        this.tools.set(tool.name, tool);

        // Map all commands to this tool
        for (const cmd of tool.commands) {
            const name = cmd.replace(/^\//, "");
            const existing = this.commandMap.get(name);
            if (existing) {
                throw new Error(
                    `Duplicate command ${cmd}: already registered by ${existing.name}`,
                );
            }
            this.commandMap.set(name, tool);
        }

        // Register pricing with CreditService
        registerToolPricing(tool.name, {
            credits: tool.credits,
            freeDaily: tool.freeDaily,
            capability: tool.capability,
            defaultModel: tool.defaultModel,
        });
    }

    /** Get a tool by name */
    getTool(name: string): ToolDefinition | undefined {
        return this.tools.get(name);
    }

    /** Get a tool by command (e.g., "search" or "s") */
    getToolByCommand(command: string): ToolDefinition | undefined {
        return this.commandMap.get(command);
    }

    /** Get all registered tools */
    getTools(): ToolDefinition[] {
        return [...this.tools.values()];
    }

    /** Generate LLM function-calling schemas for all tools */
    getLLMToolSchemas(disabledTools: Iterable<string> = []): LLMToolSchema[] {
        const schemas: LLMToolSchema[] = [];
        const disabled = new Set(disabledTools);

        for (const tool of this.tools.values()) {
            if (disabled.has(tool.name)) continue;

            // Convert Zod schema to JSON Schema for the LLM
            const zodSchema = tool.parameters;
            const jsonSchema = zodToJsonSchema(zodSchema);

            schemas.push({
                name: tool.name,
                description: tool.description,
                parameters: jsonSchema,
            });
        }

        return schemas;
    }

    /** Generate LLM function schemas for tools safe to call without explicit command intent. */
    getAutoCallableLLMToolSchemas(
        disabledTools: Iterable<string> = [],
    ): LLMToolSchema[] {
        const schemas: LLMToolSchema[] = [];
        const disabled = new Set(disabledTools);

        for (const tool of this.tools.values()) {
            if (!tool.allowAutoCall) continue;
            if (disabled.has(tool.name)) continue;
            schemas.push({
                name: tool.name,
                description: tool.description,
                parameters: zodToJsonSchema(tool.parameters),
            });
        }

        return schemas;
    }

    /** Generate help text grouped by category (Telegram HTML) */
    getHelpText(t?: Translator): string {
        const grouped = new Map<ToolCategory, ToolDefinition[]>();

        for (const tool of this.tools.values()) {
            const list = grouped.get(tool.category) ?? [];
            list.push(tool);
            grouped.set(tool.category, list);
        }

        const sections: string[] = [];

        for (const category of CATEGORY_ORDER) {
            const tools = grouped.get(category);
            if (!tools || tools.length === 0) continue;

            const emoji = CATEGORY_EMOJI[category];
            const label = escapeHtml(
                t
                    ? t(CATEGORY_LABEL_KEYS[category])
                    : CATEGORY_LABELS[category],
            );
            const lines = tools
                .map((tool) => {
                    if (tool.commands.length === 0) return null; // skip agent-only tools
                    const cmds = tool.commands.join(", ");
                    const description = escapeHtml(
                        t ? t(tool.helpText) : tool.description,
                    );
                    const cost = escapeHtml(formatToolCost(tool, t));
                    return `  ${cmds} — ${description} · <i>${cost}</i>`;
                })
                .filter(Boolean);

            if (lines.length === 0) continue;
            sections.push(`${emoji} <b>${label}</b>\n${lines.join("\n")}`);
        }

        return sections.join("\n\n");
    }

    /** Get BotCommand array for setMyCommands */
    getBotCommands(): Array<{ command: string; description: string }> {
        const commands: Array<{ command: string; description: string }> = [];

        for (const tool of this.tools.values()) {
            const primary = tool.commands[0];
            if (!primary) continue;
            commands.push({
                command: primary.replace(/^\//, ""),
                description: tool.description,
            });
        }

        return commands;
    }

    /** Auto-generate grammY command handlers for all tools with commands */
    registerCommandHandlers(bot: Bot<DerpContext>): void {
        for (const tool of this.tools.values()) {
            if (tool.commands.length === 0) continue;

            const commandNames = tool.commands.map((c) => c.replace(/^\//, ""));

            bot.command(commandNames, async (ctx) => {
                if (!ctx.dbUser || !ctx.dbChat || !ctx.creditService) return;

                const commandStart = Date.now();
                if (isToolDisabled(ctx.dbUser.preferences, tool.name)) {
                    const primaryCmd = tool.commands[0] ?? tool.name;
                    const text = ctx.t("tool-disabled", { tool: primaryCmd });
                    await replyMarkdownAndPersist(
                        ctx,
                        [text],
                        {
                            message_thread_id: ctx.message?.message_thread_id,
                            reply_to_message_id: ctx.message?.message_id,
                        },
                        buildCommandMetadata(tool, ctx, commandStart),
                        [text],
                    );
                    return;
                }

                const input = ctx.match ?? "";
                const command = ctx.message?.text
                    ?.match(/^\/([^\s@]+)/)?.[1]
                    ?.toLowerCase();
                const parsed = parseToolParams(tool, input, command);
                const threadId = ctx.message?.message_thread_id ?? null;
                const replyToMessageId = ctx.message?.message_id ?? null;
                const triggerReplyToMessageId =
                    ctx.message?.reply_to_message?.message_id ?? null;
                const replyOptions = replyOptionsFor(
                    threadId,
                    replyToMessageId,
                );

                if (!parsed.ok) {
                    await replyMarkdownAndPersist(
                        ctx,
                        [parsed.usage],
                        replyOptions,
                        buildCommandMetadata(tool, ctx, commandStart),
                        [parsed.usage],
                    );
                    return;
                }

                const media = await extractTriggerMedia(ctx);
                const idempotencyKey =
                    ctx.chat && ctx.message
                        ? `tool:${tool.name}:cmd:${ctx.chat.id}:${ctx.message.message_id}`
                        : undefined;
                const preflight = await ctx.creditService.checkToolAccess(
                    tool.name,
                );
                if (shouldConfirmToolSpend(preflight)) {
                    await this.requestToolConfirmation(
                        ctx,
                        tool,
                        parsed.params,
                        preflight,
                        {
                            idempotencyKey,
                            media,
                            replyOptions,
                            threadId,
                            replyToMessageId,
                            triggerReplyToMessageId,
                        },
                    );
                    return;
                }

                const toolCtx = await createToolExecutionContext(ctx, tool, {
                    commandStart,
                    threadId,
                    replyToMessageId,
                    triggerReplyToMessageId,
                    replyMedia: media,
                    idempotencyKey,
                });
                const result = await executeWithCreditGate(
                    tool,
                    parsed.params,
                    toolCtx,
                );
                await sendToolResult(
                    ctx,
                    tool,
                    commandStart,
                    result,
                    replyOptions,
                );
            });
        }

        this.registerConfirmationHandlers(bot);
    }

    private async requestToolConfirmation(
        ctx: DerpContext,
        tool: ToolDefinition,
        params: unknown,
        creditResult: CreditCheckResult,
        options: {
            idempotencyKey?: string;
            media: MediaAttachment[];
            replyOptions: ReplyOptions;
            threadId?: number | null;
            replyToMessageId?: number | null;
            triggerReplyToMessageId?: number | null;
        },
    ): Promise<void> {
        if (!ctx.dbUser || !ctx.dbChat || !ctx.chat) return;

        const pendingSource = creditResult.source === "chat" ? "chat" : "user";
        const idempotencyKey =
            options.idempotencyKey ??
            `tool:${tool.name}:pending:${ctx.chat.id}:${ctx.from?.id ?? "unknown"}:${Date.now()}`;
        const id = await createPendingToolConfirmation(ctx.db, {
            userId: ctx.dbUser.id,
            chatId: ctx.dbChat.id,
            telegramChatId: ctx.chat.id,
            messageId: options.replyToMessageId ?? null,
            threadId: options.threadId ?? null,
            toolName: tool.name,
            params: params as Record<string, unknown>,
            cost: creditResult.creditsToDeduct,
            source: pendingSource,
            idempotencyKey,
            expiresAt: new Date(Date.now() + TOOL_CONFIRM_TTL_MS),
            meta: {
                triggerMedia: pendingMediaInfo(options.media),
                triggerReplyToMessageId:
                    options.triggerReplyToMessageId ?? null,
            },
        });

        const source =
            pendingSource === "chat"
                ? ctx.t("tool-confirm-source-chat")
                : ctx.t("tool-confirm-source-user");
        const keyboard = new InlineKeyboard()
            .text(ctx.t("tool-confirm-run"), callbackData("run", id))
            .text(ctx.t("tool-confirm-cancel"), callbackData("cancel", id));

        await replyHtml(
            ctx,
            ctx.t(
                isGroupChat(ctx) && pendingSource === "user"
                    ? "tool-confirm-message-private"
                    : "tool-confirm-message",
                {
                    tool: primaryCommand(tool),
                    cost: creditResult.creditsToDeduct,
                    source,
                    remaining: creditResult.creditsRemaining ?? 0,
                },
            ),
            { ...options.replyOptions, reply_markup: keyboard },
        );
    }

    private registerConfirmationHandlers(bot: Bot<DerpContext>): void {
        bot.callbackQuery(/^tool_confirm:run:([0-9a-f-]+)$/i, async (ctx) => {
            const id = ctx.match[1];
            if (!id || !ctx.dbUser || !ctx.dbChat || !ctx.creditService) {
                await ctx.answerCallbackQuery(ctx.t("error-generic"));
                return;
            }

            const existing = await getPendingToolConfirmation(ctx.db, id);
            if (existing && existing.userId !== ctx.dbUser.id) {
                await ctx.answerCallbackQuery(ctx.t("tool-confirm-owner-only"));
                return;
            }

            const pending = await claimPendingToolConfirmation(ctx.db, {
                id,
                userId: ctx.dbUser.id,
            });
            if (!pending || pending.chatId !== ctx.dbChat.id) {
                await ctx.answerCallbackQuery(ctx.t("tool-confirm-expired"));
                await editCallbackMessageHtml(
                    ctx,
                    ctx.t("tool-confirm-expired"),
                );
                return;
            }

            const tool = this.getTool(pending.toolName);
            if (!tool) {
                await ctx.answerCallbackQuery(ctx.t("error-generic"));
                await editCallbackMessageHtml(
                    ctx,
                    ctx.t("tool-confirm-invalid"),
                );
                return;
            }

            const parsed = tool.parameters.safeParse(pending.params);
            if (!parsed.success) {
                await ctx.answerCallbackQuery(ctx.t("error-generic"));
                await editCallbackMessageHtml(
                    ctx,
                    ctx.t("tool-confirm-invalid"),
                );
                return;
            }

            const commandStart = Date.now();
            const replyOptions = replyOptionsFor(
                pending.threadId,
                pending.messageId,
            );
            await ctx.answerCallbackQuery(
                ctx.t("tool-confirm-running-alert", {
                    tool: primaryCommand(tool),
                }),
            );
            await editCallbackMessageHtml(
                ctx,
                ctx.t("tool-confirm-running", { tool: primaryCommand(tool) }),
            );

            let media: MediaAttachment[];
            try {
                media = await downloadPendingMedia(
                    ctx,
                    readPendingMediaInfo(pending.meta),
                );
            } catch (err) {
                logger.warn("tool_confirmation_media_download_failed", {
                    tool: tool.name,
                    error: err instanceof Error ? err.message : String(err),
                });
                await editCallbackMessageHtml(
                    ctx,
                    ctx.t("tool-confirm-failed"),
                );
                return;
            }

            const toolCtx = await createToolExecutionContext(ctx, tool, {
                commandStart,
                threadId: pending.threadId,
                replyToMessageId: pending.messageId,
                triggerReplyToMessageId: readTriggerReplyToMessageId(
                    pending.meta,
                ),
                replyMedia: media,
                idempotencyKey: pending.idempotencyKey,
                expectedCreditSource:
                    pending.source === "chat" || pending.source === "user"
                        ? pending.source
                        : undefined,
                expectedCreditsToDeduct: pending.cost,
            });
            const result = await executeWithCreditGate(
                tool,
                parsed.data,
                toolCtx,
            );
            await sendToolResult(ctx, tool, commandStart, result, replyOptions);
            await editCallbackMessageHtml(
                ctx,
                ctx.t(
                    result.error && result.billableFailure
                        ? "tool-confirm-billable-failed"
                        : result.error
                          ? "tool-confirm-failed"
                          : "tool-confirm-done",
                    {
                        tool: primaryCommand(tool),
                    },
                ),
            );
        });

        bot.callbackQuery(
            /^tool_confirm:cancel:([0-9a-f-]+)$/i,
            async (ctx) => {
                const id = ctx.match[1];
                if (!id || !ctx.dbUser) {
                    await ctx.answerCallbackQuery(ctx.t("error-generic"));
                    return;
                }

                const existing = await getPendingToolConfirmation(ctx.db, id);
                if (existing && existing.userId !== ctx.dbUser.id) {
                    await ctx.answerCallbackQuery(
                        ctx.t("tool-confirm-owner-only"),
                    );
                    return;
                }

                const cancelled = await cancelPendingToolConfirmation(ctx.db, {
                    id,
                    userId: ctx.dbUser.id,
                });
                const key = cancelled
                    ? "tool-confirm-cancelled"
                    : "tool-confirm-expired";
                await ctx.answerCallbackQuery(ctx.t(key));
                await editCallbackMessageHtml(ctx, ctx.t(key));
            },
        );
    }
}

/** Singleton registry */
export const toolRegistry = new ToolRegistry();
