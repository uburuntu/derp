/** Inline mode — answer with complete article results, never permanent placeholders. */

import { Composer, InlineQueryResultBuilder } from "grammy";
import type { DerpContext } from "../bot/context";
import { logger } from "../common/observability";
import { config, getGoogleApiKeys } from "../config";
import { GoogleLLMProvider } from "../llm/providers/google";
import { getDefaultModel, ModelCapability, ModelTier } from "../llm/registry";

const inlineComposer = new Composer<DerpContext>();
const INLINE_MIN_QUERY_LENGTH = 8;
const INLINE_THROTTLE_MS = 8_000;
const INLINE_CACHE_MS = 10 * 60 * 1000;

const inlineCache = new Map<
	string,
	{ text: string; generatedAt: number; lastRequestedAt: number }
>();

// ── Inline query ────────────────────────────────────────────────────────────

inlineComposer.on("inline_query", async (ctx) => {
	const query = ctx.inlineQuery.query.trim();
	if (!query) {
		await ctx.answerInlineQuery([]);
		return;
	}

	const responseText = await getInlineAnswer(query, ctx);
	const result = InlineQueryResultBuilder.article(
		`derp:${ctx.inlineQuery.id}`,
		ctx.t("inline-title"),
	).text(responseText);

	await ctx.answerInlineQuery([result], {
		cache_time: 3,
		is_personal: true,
	});
});

async function getInlineAnswer(
	query: string,
	ctx: DerpContext,
): Promise<string> {
	if (query.length < INLINE_MIN_QUERY_LENGTH) {
		return ctx.t("inline-placeholder");
	}

	const userId = ctx.from?.id ?? 0;
	const cacheKey = `${userId}:${query}`;
	const cached = inlineCache.get(cacheKey);
	if (cached && Date.now() - cached.generatedAt < INLINE_CACHE_MS) {
		cached.lastRequestedAt = Date.now();
		return cached.text;
	}

	const userEntries = [...inlineCache.entries()].filter(([key]) =>
		key.startsWith(`${userId}:`),
	);
	const lastRequestAt = Math.max(
		0,
		...userEntries.map(([, entry]) => entry.lastRequestedAt),
	);
	if (Date.now() - lastRequestAt < INLINE_THROTTLE_MS) {
		return ctx.t("inline-wait");
	}

	const text = await generateInlineAnswer(query, ctx);
	inlineCache.set(cacheKey, {
		text,
		generatedAt: Date.now(),
		lastRequestedAt: Date.now(),
	});
	pruneInlineCache();
	return text;
}

async function generateInlineAnswer(
	query: string,
	ctx: DerpContext,
): Promise<string> {
	// Inline mode has no chat-scoped CreditService, so keep it on the free model
	// until a dedicated inline credit policy exists.
	const model = getDefaultModel(ModelCapability.TEXT, ModelTier.FREE);

	const provider = new GoogleLLMProvider(
		getGoogleApiKeys(config),
		config.googleApiPaidKey,
	);

	try {
		const result = await provider.chat({
			model: model.id,
			systemPrompt:
				"You are Derp, a concise AI assistant. Answer the user's question directly. Keep it under 200 words.",
			messages: [{ role: "user", content: query }],
			timeoutMs: 4_500,
		});

		return result.text || ctx.t("inline-error");
	} catch (err) {
		logger.error("inline_generation_failed", {
			error: err instanceof Error ? err.message : String(err),
			query: query.slice(0, 100),
		});
		return ctx.t("inline-error");
	}
}

function pruneInlineCache(): void {
	const cutoff = Date.now() - INLINE_CACHE_MS;
	for (const [key, value] of inlineCache.entries()) {
		if (value.generatedAt < cutoff) {
			inlineCache.delete(key);
		}
	}
}

export { inlineComposer };
