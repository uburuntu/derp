/** Help handler — auto-generated from ToolRegistry */

import { Composer } from "grammy";
import type { DerpContext } from "../bot/context";
import { toolRegistry } from "../tools/registry";

const helpComposer = new Composer<DerpContext>();

helpComposer.command("help", async (ctx) => {
	const helpText = toolRegistry.getHelpText();
	// Help text is already Telegram HTML from the registry
	const html =
		`🤖 <b>Derp</b>\n\n${helpText}\n\n` +
		`<i>Just describe what you need — I'll pick the right tool automatically.</i>\n\n` +
		`⚙️ <b>Other</b>\n` +
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
