/** Executor — fires individual reminders (plain text or LLM mode) */

import type { Bot } from "grammy";
import type { DerpContext } from "../bot/context";
import { derpMetrics, logger, withSpan } from "../common/observability";
import { config, getGoogleApiKeys } from "../config";
import type { Database } from "../db/connection";
import {
	markReminderCompleted,
	markReminderFailed,
	updateNextFireAt,
} from "../db/queries/reminders";
import type { Reminder } from "../db/schema";
import { GoogleLLMProvider } from "../llm/providers/google";
import { parseCronToNextDate } from "./cron";

/** Execute a single reminder */
export async function executeReminder(
	db: Database,
	bot: Bot<DerpContext>,
	reminder: Reminder,
	isStartup: boolean,
): Promise<void> {
	// Resolve the chat's telegram ID from the DB chat UUID
	// We need the chat's telegram_id to send messages
	const chat = await getChatByTelegramIdFromUuid(db, reminder.chatId);
	if (!chat) {
		logger.error("reminder_chat_not_found", { reminderId: reminder.id });
		await markReminderFailed(db, reminder.id, "Chat not found");
		return;
	}

	const delayNote = isStartup ? "\n(delayed — bot was restarting)" : "";

	try {
		if (reminder.usesLlm && reminder.prompt) {
			// LLM mode — run an agent call with the prompt
			const provider = new GoogleLLMProvider(
				getGoogleApiKeys(config),
				config.googleApiPaidKey,
			);

			const result = await provider.chat({
				model: "gemini-2.5-flash",
				systemPrompt:
					"You are Derp, an AI assistant executing a scheduled reminder. " +
					"Follow the prompt instructions. Be concise and helpful.",
				messages: [{ role: "user", content: reminder.prompt }],
				timeoutMs: 30_000,
			});

			const responseText = result.text || reminder.description;
			await bot.api.sendMessage(
				chat.telegramId,
				`🔔 ${responseText}${delayNote}`,
				{
					message_thread_id: reminder.threadId ?? undefined,
					reply_to_message_id: reminder.replyToMessageId ?? undefined,
				},
			);
		} else if (reminder.message) {
			// Plain mode — send the stored message
			await bot.api.sendMessage(
				chat.telegramId,
				`🔔 ${reminder.message}${delayNote}`,
				{
					message_thread_id: reminder.threadId ?? undefined,
					reply_to_message_id: reminder.replyToMessageId ?? undefined,
				},
			);
		} else {
			// Fallback — send just the description
			await bot.api.sendMessage(
				chat.telegramId,
				`🔔 Reminder: ${reminder.description}${delayNote}`,
				{
					message_thread_id: reminder.threadId ?? undefined,
					reply_to_message_id: reminder.replyToMessageId ?? undefined,
				},
			);
		}

		// Update status based on type
		if (reminder.isRecurring && reminder.cronExpression) {
			const nextFire = parseCronToNextDate(reminder.cronExpression);
			if (nextFire) {
				await updateNextFireAt(db, reminder.id, nextFire);
			} else {
				await markReminderCompleted(db, reminder.id);
			}
		} else {
			await markReminderCompleted(db, reminder.id);
		}

		derpMetrics.remindersFired.add(1, {
			recurring: String(reminder.isRecurring),
			uses_llm: String(reminder.usesLlm),
		});

		logger.info("reminder_fired", {
			reminderId: reminder.id,
			isRecurring: reminder.isRecurring,
			usesLlm: reminder.usesLlm,
			isStartup,
		});
	} catch (err) {
		const errorMsg = err instanceof Error ? err.message : String(err);
		logger.error("reminder_execution_failed", {
			reminderId: reminder.id,
			error: errorMsg,
		});

		// Retry once
		try {
			if (reminder.message) {
				await bot.api.sendMessage(
					chat.telegramId,
					`🔔 ${reminder.message}${delayNote}`,
				);
			}
			if (reminder.isRecurring && reminder.cronExpression) {
				const nextFire = parseCronToNextDate(reminder.cronExpression);
				if (nextFire) {
					await updateNextFireAt(db, reminder.id, nextFire);
				} else {
					await markReminderFailed(db, reminder.id, errorMsg);
				}
			} else {
				await markReminderCompleted(db, reminder.id);
			}
		} catch {
			await markReminderFailed(db, reminder.id, errorMsg);
		}
	}
}

/** Helper: get a chat by its DB UUID (not telegram ID) */
async function getChatByTelegramIdFromUuid(
	db: Database,
	chatUuid: string,
): Promise<{ telegramId: number } | null> {
	const { eq } = await import("drizzle-orm");
	const { chats } = await import("../db/schema");
	const [row] = await db
		.select({ telegramId: chats.telegramId })
		.from(chats)
		.where(eq(chats.id, chatUuid))
		.limit(1);
	return row ?? null;
}
