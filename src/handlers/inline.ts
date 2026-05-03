/** Inline mode — answer with complete article results, never permanent placeholders. */

import { Composer, InlineQueryResultBuilder } from "grammy";
import type { DerpContext } from "../bot/context";
import { logger } from "../common/observability";
import { config, getGoogleApiKeys } from "../config";
import { GoogleLLMProvider } from "../llm/providers/google";
import { getDefaultModel, ModelCapability, ModelTier } from "../llm/registry";

const inlineComposer = new Composer<DerpContext>();

// ── Inline query ────────────────────────────────────────────────────────────

inlineComposer.on("inline_query", async (ctx) => {
	const query = ctx.inlineQuery.query.trim();
	if (!query) {
		await ctx.answerInlineQuery([]);
		return;
	}

	const responseText = await generateInlineAnswer(query, ctx);
	const result = InlineQueryResultBuilder.article(
		`derp:${ctx.inlineQuery.id}`,
		ctx.t("inline-title"),
	).text(responseText);

	await ctx.answerInlineQuery([result], {
		cache_time: 3,
		is_personal: true,
	});
});

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

export { inlineComposer };
