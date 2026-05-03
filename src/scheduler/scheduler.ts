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
import { scrubRetention } from "../db/queries/retention";
import { executeReminder } from "./executor";

let intervalHandle: ReturnType<typeof setInterval> | null = null;
let isProcessing = false;
const PROCESSING_STALE_MS = 15 * 60 * 1000;
const RETENTION_INTERVAL_MS = 24 * 60 * 60 * 1000;

interface SchedulerStatus {
	running: boolean;
	processing: boolean;
	intervalMs: number | null;
	startedAt: string | null;
	lastTickAt: string | null;
	lastSuccessAt: string | null;
	lastError: string | null;
	lastDueCount: number | null;
	lastRetentionAt: string | null;
}

const schedulerStatus: SchedulerStatus = {
	running: false,
	processing: false,
	intervalMs: null,
	startedAt: null,
	lastTickAt: null,
	lastSuccessAt: null,
	lastError: null,
	lastDueCount: null,
	lastRetentionAt: null,
};

/** Start the reminder scheduler */
export function startScheduler(
	db: Database,
	bot: Bot<DerpContext>,
	intervalMs: number,
): void {
	schedulerStatus.running = true;
	schedulerStatus.intervalMs = intervalMs;
	schedulerStatus.startedAt = new Date().toISOString();
	schedulerStatus.lastError = null;
	logger.info("scheduler_started", { intervalMs });

	// Fire overdue reminders on startup
	processAndRecord(db, bot, true).catch((err) => {
		logger.error("scheduler_startup_failed", {
			error: err instanceof Error ? err.message : String(err),
		});
	});

	// Poll at configured interval
	intervalHandle = setInterval(() => {
		processAndRecord(db, bot, false).catch((err) => {
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
	}
	schedulerStatus.running = false;
	schedulerStatus.processing = false;
	logger.info("scheduler_stopped");
}

export function getSchedulerHealth(): {
	ready: boolean;
	error?: string;
	details: Record<string, unknown>;
} {
	const intervalMs = schedulerStatus.intervalMs ?? 60_000;
	const startedAt = schedulerStatus.startedAt
		? Date.parse(schedulerStatus.startedAt)
		: 0;
	const lastTickAt = schedulerStatus.lastTickAt
		? Date.parse(schedulerStatus.lastTickAt)
		: 0;
	const recentlyStarted =
		startedAt > 0 && Date.now() - startedAt < intervalMs * 2;
	const recentlyTicked =
		lastTickAt > 0 &&
		Date.now() - lastTickAt < Math.max(intervalMs * 3, 180_000);
	const ready =
		schedulerStatus.running &&
		(recentlyStarted || recentlyTicked) &&
		!schedulerStatus.lastError;

	return {
		ready,
		error: ready
			? undefined
			: (schedulerStatus.lastError ?? "scheduler_not_running_or_stale"),
		details: { ...schedulerStatus },
	};
}

async function processAndRecord(
	db: Database,
	bot: Bot<DerpContext>,
	isStartup: boolean,
): Promise<void> {
	schedulerStatus.lastTickAt = new Date().toISOString();
	try {
		await processReminders(db, bot, isStartup);
		await runRetentionIfDue(db);
		schedulerStatus.lastSuccessAt = new Date().toISOString();
		schedulerStatus.lastError = null;
	} catch (err) {
		schedulerStatus.lastError =
			err instanceof Error ? err.message : String(err);
		throw err;
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
	schedulerStatus.processing = true;
	try {
		const released = await releaseStaleProcessingReminders(
			db,
			new Date(Date.now() - PROCESSING_STALE_MS),
		);
		if (released > 0) {
			logger.warn("scheduler_stale_reminders_released", { count: released });
		}

		const dueReminders = await getDueReminders(db);
		schedulerStatus.lastDueCount = dueReminders.length;
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
		schedulerStatus.processing = false;
	}
}

async function runRetentionIfDue(db: Database): Promise<void> {
	const lastRetentionMs = schedulerStatus.lastRetentionAt
		? Date.parse(schedulerStatus.lastRetentionAt)
		: 0;
	if (
		lastRetentionMs > 0 &&
		Date.now() - lastRetentionMs < RETENTION_INTERVAL_MS
	) {
		return;
	}

	const result = await scrubRetention(db);
	schedulerStatus.lastRetentionAt = new Date().toISOString();
	if (result.messagesScrubbed > 0 || result.ledgerRowsScrubbed > 0) {
		logger.info("retention_scrubbed", { ...result });
	}
}
