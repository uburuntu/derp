/** Executor — fires individual reminders (plain text or LLM mode) */

import { eq } from "drizzle-orm";
import type { Bot } from "grammy";
import type { DerpContext } from "../bot/context";
import { derpMetrics, logger } from "../common/observability";
import { config, getGoogleApiKeys } from "../config";
import type { Database } from "../db/connection";
import {
	deductChatCredits,
	deductUserCredits,
	getBalances,
} from "../db/queries/credits";
import {
	markReminderCompleted,
	markReminderFailed,
	updateNextFireAt,
} from "../db/queries/reminders";
import {
	type Chat,
	chats,
	type Reminder,
	type User,
	users,
} from "../db/schema";
import { GoogleLLMProvider } from "../llm/providers/google";
import { parseCronToNextDate } from "./cron";

const LLM_REMINDER_COST = 1;

async function reserveLlmReminderCredit(
	db: Database,
	chat: Chat,
	user: User,
	reminder: Reminder,
): Promise<{ ok: true } | { ok: false; reason: string }> {
	const { userCredits, chatCredits } = await getBalances(
		db,
		user.telegramId,
		chat.telegramId,
	);
	const idempotencyKey = `reminder:${reminder.id}:fire:${reminder.fireCount + 1}:llm`;
	const meta = {
		reminderId: reminder.id,
		description: reminder.description,
	};

	try {
		if (chatCredits >= LLM_REMINDER_COST) {
			await deductChatCredits(
				db,
				chat.id,
				user.id,
				LLM_REMINDER_COST,
				"reminder_llm",
				"gemini-2.5-flash",
				idempotencyKey,
				meta,
			);
			return { ok: true };
		}

		if (userCredits >= LLM_REMINDER_COST) {
			await deductUserCredits(
				db,
				user.id,
				LLM_REMINDER_COST,
				"reminder_llm",
				"gemini-2.5-flash",
				idempotencyKey,
				meta,
			);
			return { ok: true };
		}
	} catch (err) {
		return {
			ok: false,
			reason: err instanceof Error ? err.message : String(err),
		};
	}

	return {
		ok: false,
		reason: `LLM reminders need ${LLM_REMINDER_COST} credit`,
	};
}

async function sendReminderMessage(
	bot: Bot<DerpContext>,
	chat: Chat,
	reminder: Reminder,
	delayNote: string,
): Promise<void> {
	const messageOptions = {
		message_thread_id: reminder.threadId ?? undefined,
		reply_to_message_id: reminder.replyToMessageId ?? undefined,
	};

	if (reminder.usesLlm && reminder.prompt) {
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

		const responseText = result.text?.trim();
		if (!responseText) {
			throw new Error("LLM reminder returned an empty response");
		}

		await bot.api.sendMessage(
			chat.telegramId,
			`🔔 ${responseText}${delayNote}`,
			messageOptions,
		);
		return;
	}

	if (reminder.message) {
		await bot.api.sendMessage(
			chat.telegramId,
			`🔔 ${reminder.message}${delayNote}`,
			messageOptions,
		);
		return;
	}

	await bot.api.sendMessage(
		chat.telegramId,
		`🔔 Reminder: ${reminder.description}${delayNote}`,
		messageOptions,
	);
}

async function markDelivered(db: Database, reminder: Reminder): Promise<void> {
	if (reminder.isRecurring && reminder.cronExpression) {
		const nextFire = parseCronToNextDate(reminder.cronExpression);
		if (nextFire) {
			await updateNextFireAt(db, reminder.id, nextFire);
		} else {
			await markReminderCompleted(db, reminder.id);
		}
		return;
	}

	await markReminderCompleted(db, reminder.id);
}

function recordReminderFired(reminder: Reminder, isStartup: boolean): void {
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
}

/** Execute a single reminder */
export async function executeReminder(
	db: Database,
	bot: Bot<DerpContext>,
	reminder: Reminder,
	isStartup: boolean,
): Promise<void> {
	const chat = await getChatByUuid(db, reminder.chatId);
	if (!chat) {
		logger.error("reminder_chat_not_found", { reminderId: reminder.id });
		await markReminderFailed(db, reminder.id, "Chat not found");
		return;
	}

	if (reminder.usesLlm) {
		const messageOptions = {
			message_thread_id: reminder.threadId ?? undefined,
			reply_to_message_id: reminder.replyToMessageId ?? undefined,
		};

		if (reminder.isRecurring) {
			await bot.api.sendMessage(
				chat.telegramId,
				"🔔 Recurring LLM reminders are disabled. Create a plain recurring reminder or a one-time LLM reminder.",
				messageOptions,
			);
			await markReminderFailed(
				db,
				reminder.id,
				"Recurring LLM reminders disabled",
			);
			return;
		}

		const user = await getUserByUuid(db, reminder.userId);
		if (!user) {
			await markReminderFailed(db, reminder.id, "User not found");
			return;
		}

		const reservation = await reserveLlmReminderCredit(
			db,
			chat,
			user,
			reminder,
		);
		if (!reservation.ok) {
			await bot.api.sendMessage(
				chat.telegramId,
				`🔔 LLM reminder skipped: ${reservation.reason}. Use /buy to top up.`,
				messageOptions,
			);
			await markReminderFailed(db, reminder.id, reservation.reason);
			return;
		}
	}

	const delayNote = isStartup ? "\n(delayed — bot was restarting)" : "";

	try {
		await sendReminderMessage(bot, chat, reminder, delayNote);
		await markDelivered(db, reminder);
		recordReminderFired(reminder, isStartup);
	} catch (err) {
		const errorMsg = err instanceof Error ? err.message : String(err);
		logger.error("reminder_execution_failed", {
			reminderId: reminder.id,
			error: errorMsg,
		});

		// Retry once
		try {
			await sendReminderMessage(bot, chat, reminder, delayNote);
			await markDelivered(db, reminder);
			recordReminderFired(reminder, isStartup);
		} catch (retryErr) {
			const retryMsg =
				retryErr instanceof Error ? retryErr.message : String(retryErr);
			await markReminderFailed(
				db,
				reminder.id,
				`${errorMsg}; retry: ${retryMsg}`,
			);
		}
	}
}

/** Helper: get a chat by its DB UUID (not telegram ID) */
async function getChatByUuid(
	db: Database,
	chatUuid: string,
): Promise<Chat | null> {
	const [row] = await db
		.select()
		.from(chats)
		.where(eq(chats.id, chatUuid))
		.limit(1);
	return row ?? null;
}

async function getUserByUuid(
	db: Database,
	userUuid: string,
): Promise<User | null> {
	const [row] = await db
		.select()
		.from(users)
		.where(eq(users.id, userUuid))
		.limit(1);
	return row ?? null;
}
