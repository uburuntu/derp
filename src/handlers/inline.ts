/** Inline mode — deferred generation with placeholder → edit pattern */

import { Composer, InlineQueryResultBuilder } from "grammy";
import type { DerpContext } from "../bot/context";
import { logger } from "../common/observability";
import { config, getGoogleApiKeys } from "../config";
import { GoogleLLMProvider } from "../llm/providers/google";
import { getDefaultModel, ModelCapability, ModelTier } from "../llm/registry";

const inlineComposer = new Composer<DerpContext>();

// ── Inline query — return placeholder result ────────────────────────────────

inlineComposer.on("inline_query", async (ctx) => {
	const query = ctx.inlineQuery.query.trim();
	if (!query) {
		await ctx.answerInlineQuery([]);
		return;
	}

	const result = InlineQueryResultBuilder.article(
		`derp:${ctx.inlineQuery.id}`,
		ctx.t("inline-title"),
	).text(ctx.t("inline-placeholder"));

	await ctx.answerInlineQuery([result], {
		cache_time: 5,
		is_personal: true,
	});
});

// ── Chosen inline result — generate and edit ────────────────────────────────

inlineComposer.on("chosen_inline_result", async (ctx) => {
	const chosen = ctx.chosenInlineResult;
	const query = chosen.query.trim();
	if (!query) return;

	const inlineMessageId = chosen.inline_message_id;
	if (!inlineMessageId) return;

	// Determine tier from user (simplified — we don't have full DB context in inline)
	const tier = ctx.dbUser?.subscriptionTier
		? ModelTier.STANDARD
		: ModelTier.FREE;
	const model = getDefaultModel(ModelCapability.TEXT, tier);

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
			timeoutMs: 15_000,
		});

		const responseText = result.text || ctx.t("inline-error");

		await ctx.api.editMessageTextInline(inlineMessageId, responseText);
	} catch (err) {
		logger.error("inline_generation_failed", {
			error: err instanceof Error ? err.message : String(err),
			query: query.slice(0, 100),
		});
		try {
			await ctx.api.editMessageTextInline(
				inlineMessageId,
				"Sorry, I couldn't generate a response. Try again.",
			);
		} catch {
			// If editing fails, nothing we can do
		}
	}
});

export { inlineComposer };
