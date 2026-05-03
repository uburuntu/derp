/** Reminders handler — /reminders list with inline cancel buttons */

import { Composer, InlineKeyboard } from "grammy";
import type { DerpContext } from "../bot/context";
import { replyHtml } from "../common/reply";
import { escapeHtml } from "../common/sanitize";
import {
	cancelReminder,
	getReminderById,
	getRemindersForChat,
} from "../db/queries/reminders";

const remindersComposer = new Composer<DerpContext>();

/** Format a date in ISO-like readable format (no locale dependency) */
function formatDate(date: Date): string {
	const pad = (n: number) => String(n).padStart(2, "0");
	return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function callbackThreadId(ctx: DerpContext): number | null {
	const message = ctx.callbackQuery?.message;
	if (!message || !("message_id" in message)) return null;
	return (message as { message_thread_id?: number }).message_thread_id ?? null;
}

remindersComposer.command("reminders", async (ctx) => {
	if (!ctx.dbChat) return;

	const threadId = ctx.message?.message_thread_id ?? null;
	const reminders = await getRemindersForChat(
		ctx.db,
		ctx.dbChat.id,
		undefined,
		threadId,
	);

	if (reminders.length === 0) {
		await replyHtml(ctx, ctx.t("reminder-none"), {
			message_thread_id: ctx.message?.message_thread_id,
			reply_to_message_id: ctx.message?.message_id,
		});
		return;
	}

	const lines: string[] = [];
	const kb = new InlineKeyboard();

	for (const [i, r] of reminders.entries()) {
		const schedule = r.isRecurring
			? `cron: ${r.cronExpression}`
			: r.fireAt
				? formatDate(r.fireAt)
				: "—";
		lines.push(
			`${i + 1}. ${escapeHtml(r.description)} — ${escapeHtml(schedule)}`,
		);
		kb.text(
			`${ctx.t("reminder-cancel-button")} #${i + 1}`,
			`cancel_reminder:${r.id}`,
		).row();
	}

	await replyHtml(ctx, lines.join("\n"), {
		reply_markup: kb,
		message_thread_id: ctx.message?.message_thread_id,
		reply_to_message_id: ctx.message?.message_id,
	});
});

remindersComposer.callbackQuery(/^cancel_reminder:(.+)$/, async (ctx) => {
	const reminderId = ctx.match[1];
	if (!reminderId) return;

	const reminder = await getReminderById(ctx.db, reminderId);
	if (!reminder || reminder.chatId !== ctx.dbChat?.id) {
		await ctx.answerCallbackQuery(ctx.t("reminder-not-found"));
		return;
	}
	if ((reminder.threadId ?? null) !== callbackThreadId(ctx)) {
		await ctx.answerCallbackQuery(ctx.t("reminder-not-found"));
		return;
	}

	if (reminder.userId !== ctx.dbUser?.id) {
		const chatMember = await ctx.getChatMember(ctx.from.id);
		if (
			chatMember.status !== "administrator" &&
			chatMember.status !== "creator"
		) {
			await ctx.answerCallbackQuery(ctx.t("reminder-no-permission"));
			return;
		}
	}

	await cancelReminder(ctx.db, reminderId);
	await ctx.answerCallbackQuery("OK");
	await ctx.editMessageText(
		ctx.t("reminder-cancelled", {
			description: escapeHtml(reminder.description),
		}),
		{ parse_mode: "HTML" },
	);
});

export { remindersComposer };
