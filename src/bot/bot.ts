/** Bot setup — create Bot instance, register middleware and plugins */

import { autoChatAction } from "@grammyjs/auto-chat-action";
import { autoRetry } from "@grammyjs/auto-retry";
import { sequentialize } from "@grammyjs/runner";
import { apiThrottler } from "@grammyjs/transformer-throttler";
import { Bot } from "grammy";
import { logger } from "../common/observability";
import { config } from "../config";
import { resetToolPricingForTests } from "../credits/service";
import type { Database } from "../db/connection";
import { adminComposer } from "../handlers/admin";
import { chatComposer } from "../handlers/chat";
import { creditsComposer } from "../handlers/credits";
import { donationsComposer } from "../handlers/donations";
import { helpComposer } from "../handlers/help";
import { infoComposer } from "../handlers/info";
import { inlineComposer } from "../handlers/inline";
import { remindersComposer } from "../handlers/reminders";
import { settingsComposer } from "../handlers/settings";
import { startComposer } from "../handlers/start";
import { i18n } from "../i18n/index";
import { errorBoundary } from "../middleware/error-boundary";
import { createHydrator } from "../middleware/hydrator";
import { loggerMiddleware } from "../middleware/logger";
import { createRateLimiter } from "../middleware/rate-limiter";
import { sessionMiddleware } from "../middleware/session";
import { loadToolDefinitions } from "../tools/loader";
import { toolRegistry } from "../tools/registry";
import type { ToolDefinition } from "../tools/types";
import type { DerpContext } from "./context";

function sequentializeKeys(ctx: DerpContext): string[] | undefined {
    if (
        ctx.callbackQuery ||
        ctx.inlineQuery ||
        ctx.chosenInlineResult ||
        ctx.preCheckoutQuery
    ) {
        return undefined;
    }
    const message = ctx.message ?? ctx.editedMessage;
    if (!message || message.successful_payment) return undefined;
    const text = message.text ?? message.caption ?? "";
    if (text.startsWith("/")) return undefined;

    const chatId = ctx.chat?.id;
    if (chatId == null) return undefined;
    const threadId = message.message_thread_id ?? 0;
    return [`chat:${chatId}:thread:${threadId}`];
}

export interface CreateBotOptions {
    tools?: ToolDefinition[];
    resetToolRegistry?: boolean;
    installRuntimeApiTransformers?: boolean;
    enableRateLimiter?: boolean;
}

export async function createBot(
    db: Database,
    options: CreateBotOptions = {},
): Promise<Bot<DerpContext>> {
    const bot = new Bot<DerpContext>(config.telegramBotToken);

    // ── API Transformers (outgoing) ──────────────────────────────────
    if (options.installRuntimeApiTransformers ?? true) {
        bot.api.config.use(autoRetry());
        bot.api.config.use(apiThrottler());
    }

    if (options.resetToolRegistry) {
        toolRegistry.resetForTests();
        resetToolPricingForTests();
    }

    // ── Middleware Stack (incoming, order matters) ───────────────────
    // 1. Error boundary — catch all errors
    bot.use(errorBoundary);
    // 2. Rate limiter — cheap per-user guard before DB work
    if (options.enableRateLimiter ?? true) {
        bot.use(createRateLimiter());
    }
    // 3. Sequentialize ordinary chat messages only; buttons/payments must answer fast.
    bot.use(sequentialize(sequentializeKeys));
    // 4. Logger — structured logging
    bot.use(loggerMiddleware);
    // 5. Hydrator — upsert user/chat/member/message
    bot.use(createHydrator(db));
    // 6. Session — load credit balances, determine tier
    bot.use(sessionMiddleware);
    // 7. Auto chat action — "typing..." indicators
    bot.use(autoChatAction());
    // 8. i18n — locale detection
    bot.use(i18n);

    // ── Register Tools ──────────────────────────────────────────────
    const tools = options.tools ?? (await loadToolDefinitions());
    for (const tool of tools) {
        toolRegistry.register(tool);
    }

    // ── Auto-generate slash command handlers for tools ──────────────
    toolRegistry.registerCommandHandlers(bot);

    // ── Register Handlers (composers) ───────────────────────────────
    bot.use(startComposer);
    bot.use(helpComposer);
    bot.use(donationsComposer);
    bot.use(creditsComposer);
    bot.use(adminComposer);
    bot.use(infoComposer);
    bot.use(remindersComposer);
    bot.use(settingsComposer);
    bot.use(inlineComposer);
    bot.on("callback_query:data", (ctx) => ctx.answerCallbackQuery());
    bot.use(chatComposer); // Must be last — catches all messages

    return bot;
}

/** Register bot commands with Telegram via setMyCommands */
export async function registerCommands(bot: Bot<DerpContext>): Promise<void> {
    const commands = toolRegistry.getBotCommands();

    // Add static commands
    const allCommands = [
        { command: "start", description: "Start the bot" },
        { command: "help", description: "Show help and available commands" },
        { command: "settings", description: "Open settings menu" },
        { command: "credits", description: "Check credit balance" },
        { command: "buy", description: "Buy credits or subscribe" },
        { command: "donate", description: "Support Derp with Telegram Stars" },
        { command: "memory_set", description: "Set chat memory" },
        { command: "memory_clear", description: "Clear chat memory" },
        { command: "reminders", description: "List active reminders" },
        { command: "info", description: "Show message generation details" },
        ...commands,
    ];

    await bot.api.setMyCommands(allCommands);
    logger.info("commands_registered", { count: allCommands.length });
}
