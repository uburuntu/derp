/** Scheduler — polls for due reminders and dispatches execution */

import type { Bot } from "grammy";
import type { DerpContext } from "../bot/context";
import { logger, withSpan } from "../common/observability";
import type { Database } from "../db/connection";
import {
	claimReminderForExecution,
	getDueReminders,
	markReminderFailed,
	releaseStaleProcessingReminders,
} from "../db/queries/reminders";
import { executeReminder } from "./executor";

let intervalHandle: ReturnType<typeof setInterval> | null = null;
let isProcessing = false;
const PROCESSING_STALE_MS = 15 * 60 * 1000;

/** Start the reminder scheduler */
export function startScheduler(
	db: Database,
	bot: Bot<DerpContext>,
	intervalMs: number,
): void {
	logger.info("scheduler_started", { intervalMs });

	// Fire overdue reminders on startup
	processReminders(db, bot, true).catch((err) => {
		logger.error("scheduler_startup_failed", {
			error: err instanceof Error ? err.message : String(err),
		});
	});

	// Poll at configured interval
	intervalHandle = setInterval(() => {
		processReminders(db, bot, false).catch((err) => {
			logger.error("scheduler_poll_failed", {
				error: err instanceof Error ? err.message : String(err),
			});
		});
	}, intervalMs);
}

/** Stop the scheduler */
export function stopScheduler(): void {
	if (intervalHandle) {
		clearInterval(intervalHandle);
		intervalHandle = null;
		logger.info("scheduler_stopped");
	}
}

/** Process all due reminders */
async function processReminders(
	db: Database,
	bot: Bot<DerpContext>,
	isStartup: boolean,
): Promise<void> {
	if (isProcessing) {
		logger.warn("scheduler_tick_skipped", { reason: "already_processing" });
		return;
	}

	isProcessing = true;
	try {
		const released = await releaseStaleProcessingReminders(
			db,
			new Date(Date.now() - PROCESSING_STALE_MS),
		);
		if (released > 0) {
			logger.warn("scheduler_stale_reminders_released", { count: released });
		}

		const dueReminders = await getDueReminders(db);
		if (dueReminders.length === 0) return;

		await withSpan(
			"scheduler.process_reminders",
			{
				"derp.scheduler.is_startup": isStartup,
				"derp.scheduler.due_count": dueReminders.length,
			},
			async () => {
				logger.info("scheduler_due_reminders", {
					count: dueReminders.length,
					isStartup,
				});

				for (const reminder of dueReminders) {
					try {
						const claimed = await claimReminderForExecution(db, reminder.id);
						if (!claimed) continue;
						await executeReminder(db, bot, claimed, isStartup);
					} catch (err) {
						logger.error("scheduler_reminder_failed", {
							reminderId: reminder.id,
							error: err instanceof Error ? err.message : String(err),
						});
						await markReminderFailed(
							db,
							reminder.id,
							err instanceof Error ? err.message : String(err),
						);
					}
				}
			},
		);
	} finally {
		isProcessing = false;
	}
}
