/** Info handler — /info command for message generation details */

import { Composer } from "grammy";
import type { DerpContext } from "../bot/context";
import { getMessageByTelegramId } from "../db/queries/messages";

const infoComposer = new Composer<DerpContext>();

infoComposer.command("info", async (ctx) => {
	if (!ctx.dbChat) return;

	const replyTo = ctx.message?.message_id;

	const repliedTo = ctx.message?.reply_to_message;
	if (!repliedTo) {
		await ctx.reply(ctx.t("info-reply-required"), {
			reply_to_message_id: replyTo,
		});
		return;
	}

	const msg = await getMessageByTelegramId(
		ctx.db,
		ctx.dbChat.id,
		repliedTo.message_id,
	);

	if (!msg) {
		await ctx.reply(ctx.t("info-not-found"), {
			reply_to_message_id: replyTo,
		});
		return;
	}

	if (msg.direction !== "out" || !msg.metadata) {
		await ctx.reply(ctx.t("info-no-details"), {
			reply_to_message_id: replyTo,
		});
		return;
	}

	const meta = msg.metadata;
	const lines: string[] = ["📊 <b>Message Info</b>\n"];

	if (meta.model) lines.push(`<b>Model:</b> ${meta.model}`);
	if (meta.tier) lines.push(`<b>Tier:</b> ${meta.tier}`);
	if (meta.inputTokens != null || meta.outputTokens != null) {
		const input = meta.inputTokens ?? 0;
		const output = meta.outputTokens ?? 0;
		const cache =
			meta.cacheHitTokens && meta.cacheHitTokens > 0
				? ` (${meta.cacheHitTokens} cached)`
				: "";
		lines.push(`<b>Tokens:</b> ${input} in / ${output} out${cache}`);
	}
	if (meta.toolsUsed && meta.toolsUsed.length > 0)
		lines.push(`<b>Tools:</b> ${meta.toolsUsed.join(", ")}`);
	if (meta.creditsSpent != null && meta.creditsSpent > 0)
		lines.push(
			`<b>Credits:</b> ${meta.creditsSpent} (${meta.creditSource ?? "unknown"})`,
		);
	if (meta.durationMs != null)
		lines.push(`<b>Duration:</b> ${meta.durationMs}ms`);

	await ctx.reply(lines.join("\n"), {
		parse_mode: "HTML",
		reply_to_message_id: repliedTo.message_id,
	});
});

export { infoComposer };
