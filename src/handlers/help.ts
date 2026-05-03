/** Help handler — auto-generated from ToolRegistry */

import { Composer } from "grammy";
import type { DerpContext } from "../bot/context";
import { toolRegistry } from "../tools/registry";

const helpComposer = new Composer<DerpContext>();

helpComposer.command("help", async (ctx) => {
	const helpText = toolRegistry.getHelpText((key, args) => ctx.t(key, args));
	// Help text is already Telegram HTML from the registry
	const html =
		`🤖 <b>Derp</b>\n\n${helpText}\n\n` +
		`<i>${ctx.t("help-footer")}</i>\n\n` +
		`⚙️ <b>${ctx.t("help-other")}</b>\n` +
		`  /credits — ${ctx.t("cmd-credits-desc")}\n` +
		`  /buy — ${ctx.t("cmd-buy-desc")}\n` +
		`  /settings — ${ctx.t("cmd-settings-desc")}\n` +
		`  /memory — ${ctx.t("cmd-memory-desc")}\n` +
		`  /reminders — ${ctx.t("cmd-reminders-desc")}\n` +
		`  /info — ${ctx.t("cmd-info-desc")}`;

	await ctx.reply(html, {
		parse_mode: "HTML",
		reply_to_message_id: ctx.message?.message_id,
	});
});

export { helpComposer };
