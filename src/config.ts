import { z } from "zod";

const configSchema = z.object({
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
	braveSearchApiKey: z.string().optional(),
	botAdminIds: z
		.string()
		.default("")
		.transform((s, ctx) => {
			if (!s) return [];
			const ids = s.split(",").map((part) => part.trim());
			const parsed = ids.map(Number);
			const invalid = ids.filter((_, index) => Number.isNaN(parsed[index]));
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
		braveSearchApiKey: process.env.BRAVE_SEARCH_API_KEY,
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
