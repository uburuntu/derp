/** Start handler — /start command and group onboarding */

import { Composer } from "grammy";
import type { DerpContext } from "../bot/context";

const startComposer = new Composer<DerpContext>();

startComposer.command("start", async (ctx) => {
    if (ctx.chat?.type !== "private") return;
    if (!ctx.dbUser) return;

    const html = `👋 <b>${ctx.t("welcome")}</b>\n\n${ctx.t("welcome-features")}`;

    await ctx.reply(html, {
        parse_mode: "HTML",
    });
});

startComposer.on("my_chat_member", async (ctx) => {
    const update = ctx.myChatMember;
    if (!update) return;

    const newStatus = update.new_chat_member.status;
    const oldStatus = update.old_chat_member.status;
    const wasAdded =
        (oldStatus === "left" || oldStatus === "kicked") &&
        (newStatus === "member" || newStatus === "administrator");

    if (!wasAdded) return;
    if (!ctx.dbChat) return;
    if (ctx.chat?.type !== "group" && ctx.chat?.type !== "supergroup") return;

    await ctx.reply(ctx.t("group-welcome"), { parse_mode: "HTML" });
});

export { startComposer };
