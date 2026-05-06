import { z } from "zod";

const configSchema = z
    .object({
        environment: z.enum(["dev", "prod"]).default("dev"),
        telegramBotToken: z.string().min(1, "TELEGRAM_BOT_TOKEN is required"),
        botUsername: z.string().default("DerpRobot"),
        databaseUrl: z.string().min(1, "DATABASE_URL is required"),
        googleApiKey: z.string().min(1, "GOOGLE_API_KEY is required"),
        googleApiKeys: z
            .string()
            .default("")
            .transform((s) => (s ? s.split(",").filter(Boolean) : [])),
        googleApiPaidKey: z.string().optional(),
        openrouterApiKey: z.string().optional(),
        openrouterPaidFallbackModel: z.string().default("openai/gpt-5.4-mini"),
        openrouterPaidFallbackAllowedModels: z
            .string()
            .default(
                "openai/gpt-5.4-mini,google/gemini-2.5-flash,google/gemini-2.5-flash-lite",
            )
            .transform((s) =>
                s
                    .split(",")
                    .map((part) => part.trim())
                    .filter(Boolean),
            ),
        braveSearchApiKey: z.string().optional(),
        freeChatDailyBotLimit: z.coerce.number().int().min(0).default(1000),
        freeSearchDailyBotLimit: z.coerce.number().int().min(0).default(500),
        freePromoMediaDailyBotLimit: z.coerce.number().int().min(0).default(0),
        botAdminIds: z
            .string()
            .default("")
            .transform((s, ctx) => {
                if (!s) return [];
                const ids = s.split(",").map((part) => part.trim());
                const parsed = ids.map(Number);
                const invalid = ids.filter((_, index) =>
                    Number.isNaN(parsed[index]),
                );
                if (invalid.length > 0) {
                    ctx.addIssue({
                        code: "custom",
                        message: `Invalid BOT_ADMIN_IDS entries: ${invalid.join(", ")}`,
                    });
                    return z.NEVER;
                }
                return parsed;
            }),
        botAdminEventsChatId: z.coerce.number().optional(),
        logfireToken: z.string().optional(),
        otelExporterOtlpEndpoint: z.string().optional(),
        otelServiceName: z.string().default("derp"),
        reminderCheckIntervalMs: z.coerce.number().default(60_000),
    })
    .superRefine((cfg, ctx) => {
        if (cfg.environment === "prod" && !cfg.logfireToken) {
            ctx.addIssue({
                code: "custom",
                path: ["logfireToken"],
                message: "LOGFIRE_TOKEN is required when ENVIRONMENT=prod",
            });
        }
        if (cfg.environment === "prod" && cfg.botAdminIds.length === 0) {
            ctx.addIssue({
                code: "custom",
                path: ["botAdminIds"],
                message: "BOT_ADMIN_IDS is required when ENVIRONMENT=prod",
            });
        }
        if (cfg.environment === "prod" && !cfg.botAdminEventsChatId) {
            ctx.addIssue({
                code: "custom",
                path: ["botAdminEventsChatId"],
                message:
                    "BOT_ADMIN_EVENTS_CHAT_ID is required when ENVIRONMENT=prod",
            });
        }
        if (cfg.environment === "prod" && !cfg.googleApiPaidKey) {
            ctx.addIssue({
                code: "custom",
                path: ["googleApiPaidKey"],
                message:
                    "GOOGLE_API_PAID_KEY is required when ENVIRONMENT=prod",
            });
        }
        if (
            cfg.openrouterApiKey &&
            !cfg.openrouterPaidFallbackAllowedModels.includes(
                cfg.openrouterPaidFallbackModel,
            )
        ) {
            ctx.addIssue({
                code: "custom",
                path: ["openrouterPaidFallbackModel"],
                message:
                    "OPENROUTER_PAID_FALLBACK_MODEL must be listed in OPENROUTER_PAID_FALLBACK_ALLOWED_MODELS",
            });
        }
    });

export type Config = z.infer<typeof configSchema>;

function loadConfig(): Config {
    const raw = {
        environment: process.env.ENVIRONMENT,
        telegramBotToken: process.env.TELEGRAM_BOT_TOKEN,
        botUsername: process.env.BOT_USERNAME,
        databaseUrl: process.env.DATABASE_URL,
        googleApiKey: process.env.GOOGLE_API_KEY,
        googleApiKeys: process.env.GOOGLE_API_KEYS,
        googleApiPaidKey: process.env.GOOGLE_API_PAID_KEY,
        openrouterApiKey: process.env.OPENROUTER_API_KEY,
        openrouterPaidFallbackModel: process.env.OPENROUTER_PAID_FALLBACK_MODEL,
        openrouterPaidFallbackAllowedModels:
            process.env.OPENROUTER_PAID_FALLBACK_ALLOWED_MODELS,
        braveSearchApiKey: process.env.BRAVE_SEARCH_API_KEY,
        freeChatDailyBotLimit: process.env.FREE_CHAT_DAILY_BOT_LIMIT,
        freeSearchDailyBotLimit: process.env.FREE_SEARCH_DAILY_BOT_LIMIT,
        freePromoMediaDailyBotLimit:
            process.env.FREE_PROMO_MEDIA_DAILY_BOT_LIMIT,
        botAdminIds: process.env.BOT_ADMIN_IDS,
        botAdminEventsChatId: process.env.BOT_ADMIN_EVENTS_CHAT_ID,
        logfireToken: process.env.LOGFIRE_TOKEN,
        otelExporterOtlpEndpoint: process.env.OTEL_EXPORTER_OTLP_ENDPOINT,
        otelServiceName: process.env.OTEL_SERVICE_NAME,
        reminderCheckIntervalMs: process.env.REMINDER_CHECK_INTERVAL_MS,
    };

    const result = configSchema.safeParse(raw);
    if (!result.success) {
        const errors = result.error.issues
            .map((i) => `  ${i.path.join(".")}: ${i.message}`)
            .join("\n");
        console.error(`Configuration error:\n${errors}`);
        process.exit(1);
    }

    return result.data;
}

/** All Google API keys (primary + extras) as a round-robin iterable */
export function getGoogleApiKeys(cfg: Config): string[] {
    const keys = [cfg.googleApiKey, ...cfg.googleApiKeys].filter(Boolean);
    return keys;
}

/** Get the bot's numeric ID from the token */
export function getBotId(cfg: Config): number {
    const [botId] = cfg.telegramBotToken.split(":");
    if (!botId) {
        throw new Error("Invalid TELEGRAM_BOT_TOKEN");
    }
    return Number.parseInt(botId, 10);
}

export const config = loadConfig();
