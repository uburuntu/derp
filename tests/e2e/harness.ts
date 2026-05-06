import type { Bot } from "grammy";
import type { Message, Update, User } from "grammy/types";
import type { DerpContext } from "../../src/bot/context";
import type { Database } from "../../src/db/connection";
import type { ToolDefinition } from "../../src/tools/types";
import { nowTelegramDate } from "./telegram-updates";

export interface ApiCall {
    method: string;
    payload: Record<string, unknown>;
    result: unknown;
}

export interface E2EHarness {
    bot: Bot<DerpContext>;
    db: Database;
    api: FakeTelegramApi;
    handle(update: Update): Promise<void>;
}

export interface CreateE2EHarnessOptions {
    tools?: ToolDefinition[];
}

export function shouldRunE2E(): boolean {
    return (
        process.env.DERP_RUN_E2E_TESTS === "1" &&
        Boolean(process.env.DATABASE_URL)
    );
}

export async function createE2EHarness(
    options: CreateE2EHarnessOptions = {},
): Promise<E2EHarness> {
    ensureE2EEnv();

    const [{ getDb }, { createBot }, { initObservability }] = await Promise.all(
        [
            import("../../src/db/connection"),
            import("../../src/bot/bot"),
            import("../../src/common/observability"),
        ],
    );

    initObservability({
        environment: "dev",
        telegramBotToken: process.env.TELEGRAM_BOT_TOKEN as string,
        botUsername: process.env.BOT_USERNAME ?? "DerpTestBot",
        databaseUrl: process.env.DATABASE_URL as string,
        googleApiKey: process.env.GOOGLE_API_KEY as string,
        googleApiKeys: [],
        googleApiPaidKey: process.env.GOOGLE_API_PAID_KEY,
        openrouterApiKey: process.env.OPENROUTER_API_KEY,
        openrouterPaidFallbackModel:
            process.env.OPENROUTER_PAID_FALLBACK_MODEL ?? "openai/gpt-5.4-mini",
        openrouterPaidFallbackAllowedModels: [
            "openai/gpt-5.4-mini",
            "google/gemini-2.5-flash",
            "google/gemini-2.5-flash-lite",
        ],
        braveSearchApiKey: process.env.BRAVE_SEARCH_API_KEY,
        freeChatDailyBotLimit: 1000,
        freeSearchDailyBotLimit: 500,
        freePromoMediaDailyBotLimit: 0,
        botAdminIds: adminIdsFromEnv(),
        botAdminEventsChatId: undefined,
        logfireToken: undefined,
        otelExporterOtlpEndpoint: undefined,
        otelServiceName: "derp-e2e",
        reminderCheckIntervalMs: 60_000,
    });

    const db = getDb(process.env.DATABASE_URL as string);
    const bot = await createBot(db, {
        tools: options.tools ?? [],
        resetToolRegistry: true,
        installRuntimeApiTransformers: false,
        enableRateLimiter: false,
    });
    const api = new FakeTelegramApi();
    const transformer: Parameters<typeof bot.api.config.use>[0] = async (
        _prev,
        method,
        payload,
    ) => api.handle(method, payload as Record<string, unknown>) as never;
    bot.api.config.use(transformer);
    await bot.init();

    return {
        bot,
        db,
        api,
        handle: (update) => bot.handleUpdate(update),
    };
}

export function ensureE2EEnv(): void {
    process.env.ENVIRONMENT ??= "dev";
    process.env.TELEGRAM_BOT_TOKEN ??= "123456:test";
    process.env.BOT_USERNAME ??= "DerpTestBot";
    process.env.GOOGLE_API_KEY ??= "test-google-key";
    process.env.BOT_ADMIN_IDS ??= "7000000001";
    process.env.PAYMENT_PAYLOAD_SECRET ??= "derp-e2e-payment-secret";
}

export function configuredAdminId(): number {
    const [adminId] = adminIdsFromEnv();
    if (!adminId) throw new Error("BOT_ADMIN_IDS must include one test admin");
    return adminId;
}

export class FakeTelegramApi {
    readonly calls: ApiCall[] = [];
    private messageId = 900_000;
    private botUser: User;

    constructor() {
        this.botUser = {
            id: botIdFromToken(),
            is_bot: true,
            first_name: "Derp",
            username: process.env.BOT_USERNAME ?? "DerpTestBot",
        };
    }

    callsFor(method: string): ApiCall[] {
        return this.calls.filter((call) => call.method === method);
    }

    lastCall(method: string): ApiCall | undefined {
        return this.callsFor(method).at(-1);
    }

    async handle(
        method: string,
        payload: Record<string, unknown>,
    ): Promise<unknown> {
        const result = this.resultFor(method, payload);
        this.calls.push({ method, payload, result });
        return result;
    }

    private resultFor(
        method: string,
        payload: Record<string, unknown>,
    ): unknown {
        switch (method) {
            case "getMe":
                return this.botUser;
            case "sendMessage":
                return this.message(payload, {
                    text: String(payload.text ?? ""),
                });
            case "sendInvoice":
                return this.message(payload, {
                    invoice: {
                        title: String(payload.title ?? ""),
                        description: String(payload.description ?? ""),
                        start_parameter: String(payload.start_parameter ?? ""),
                        currency: String(payload.currency ?? "XTR"),
                        total_amount: invoiceTotal(payload.prices),
                    },
                });
            case "createInvoiceLink":
                return `https://t.me/${this.botUser.username ?? "DerpTestBot"}?start=invoice_${this.calls.length + 1}`;
            case "answerCallbackQuery":
            case "answerPreCheckoutQuery":
            case "deleteMessage":
            case "sendChatAction":
            case "refundStarPayment":
            case "setMyCommands":
                return true;
            case "editMessageText":
                return true;
            case "getChatMember":
                return {
                    user: {
                        id: Number(payload.user_id ?? 0),
                        is_bot: false,
                        first_name: "E2E member",
                    },
                    status: "administrator",
                };
            case "getMyStarBalance":
                return { amount: 0, nanostar_amount: 0 };
            case "getChat":
                return {
                    id: Number(payload.chat_id ?? 0),
                    type: "private",
                    first_name: "E2E chat",
                };
            default:
                throw new Error(
                    `Unhandled fake Telegram API method: ${method}`,
                );
        }
    }

    private message(
        payload: Record<string, unknown>,
        extra: Partial<Message>,
    ): Message {
        this.messageId += 1;
        const chatId = Number(payload.chat_id ?? 0);
        return {
            message_id: this.messageId,
            date: nowTelegramDate(),
            chat: {
                id: chatId,
                type: chatId < 0 ? "supergroup" : "private",
                first_name: chatId < 0 ? undefined : "E2E recipient",
                title: chatId < 0 ? "E2E group" : undefined,
            },
            from: this.botUser,
            message_thread_id: payload.message_thread_id as number | undefined,
            ...extra,
        } as Message;
    }
}

function adminIdsFromEnv(): number[] {
    return (process.env.BOT_ADMIN_IDS ?? "")
        .split(",")
        .map((part) => Number(part.trim()))
        .filter((id) => Number.isFinite(id));
}

function botIdFromToken(): number {
    const [id] = (process.env.TELEGRAM_BOT_TOKEN ?? "123456:test").split(":");
    return Number.parseInt(id ?? "123456", 10);
}

function invoiceTotal(prices: unknown): number {
    if (!Array.isArray(prices)) return 0;
    return prices.reduce((sum, item) => {
        if (!item || typeof item !== "object") return sum;
        const amount = (item as { amount?: unknown }).amount;
        return sum + (typeof amount === "number" ? amount : 0);
    }, 0);
}
