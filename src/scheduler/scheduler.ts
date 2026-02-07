/** Scheduler — polls for due reminders and dispatches execution */

import type { Bot } from "grammy";
import type { DerpContext } from "../bot/context";
import { logger, withSpan } from "../common/observability";
import type { Database } from "../db/connection";
import { getDueReminders } from "../db/queries/reminders";
import { executeReminder } from "./executor";

let intervalHandle: ReturnType<typeof setInterval> | null = null;

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
					await executeReminder(db, bot, reminder, isStartup);
				} catch (err) {
					logger.error("scheduler_reminder_failed", {
						reminderId: reminder.id,
						error: err instanceof Error ? err.message : String(err),
					});
				}
			}
		},
	);
}
