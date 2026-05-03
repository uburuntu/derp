/** Remind tool — LLM-callable tool for creating, listing, and cancelling reminders */

import { z } from "zod";
import {
	cancelReminder,
	countActiveReminders,
	countRecurringReminders,
	createReminder,
	getReminderById,
	getRemindersForChat,
} from "../db/queries/reminders";
import { parseCronToNextDate, validateCron } from "../scheduler/cron";
import type { ToolContext, ToolDefinition, ToolResult } from "./types";

// ── Abuse limits ────────────────────────────────────────────────────────────

const MAX_ACTIVE_PER_CHAT = 10;
const MAX_RECURRING_PER_USER = 3;

function formatDate(date: Date): string {
	const pad = (n: number) => String(n).padStart(2, "0");
	return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

// ── Schema ──────────────────────────────────────────────────────────────────

const remindParamsSchema = z.object({
	action: z
		.enum(["create", "list", "cancel"])
		.describe(
			"Action: create a new reminder, list active reminders, or cancel one",
		),
	description: z
		.string()
		.optional()
		.describe(
			"Human-readable description of the reminder (required for create)",
		),
	message: z
		.string()
		.optional()
		.describe(
			"Plain text message to send when the reminder fires (plain mode)",
		),
	prompt: z
		.string()
		.optional()
		.describe("LLM prompt to execute when the reminder fires (LLM mode)"),
	fireAt: z
		.string()
		.optional()
		.describe(
			"ISO 8601 datetime for when to fire (e.g., 2025-01-15T14:30:00Z)",
		),
	cronExpression: z
		.string()
		.optional()
		.describe(
			"Cron expression for recurring reminders (e.g., '0 9 * * 1-5' for weekdays at 9am)",
		),
	reminderId: z
		.string()
		.optional()
		.describe("Reminder ID to cancel (required for cancel action)"),
});

type RemindParams = z.infer<typeof remindParamsSchema>;

type ReminderMetadataContext = ToolContext & {
	threadId?: number | null;
	messageThreadId?: number | null;
	replyToMessageId?: number | null;
	triggerMessageId?: number | null;
	messageId?: number | null;
};

function getOptionalNumber(
	ctx: ReminderMetadataContext,
	keys: (keyof ReminderMetadataContext)[],
): number | null {
	for (const key of keys) {
		const value = ctx[key];
		if (typeof value === "number") return value;
	}
	return null;
}

function getReminderMetadata(ctx: ToolContext): {
	threadId: number | null;
	replyToMessageId: number | null;
} {
	const metadataCtx = ctx as ReminderMetadataContext;
	return {
		threadId: getOptionalNumber(metadataCtx, ["threadId", "messageThreadId"]),
		replyToMessageId: getOptionalNumber(metadataCtx, [
			"replyToMessageId",
			"triggerMessageId",
			"messageId",
		]),
	};
}

function parseRemindCommand(input: string): RemindParams {
	const trimmed = input.trim();
	if (!trimmed || /^list$/i.test(trimmed)) {
		return { action: "list" };
	}

	const cancel = trimmed.match(/^cancel\s+(\S+)$/i);
	if (cancel?.[1]) {
		return { action: "cancel", reminderId: cancel[1] };
	}

	const at = trimmed.match(/^(?:at|create)\s+(\S+)\s+(.+)$/i);
	if (at?.[1] && at[2]) {
		return {
			action: "create",
			fireAt: at[1],
			description: at[2],
			message: at[2],
		};
	}

	const cron = trimmed.match(/^cron\s+(.+?)\s*\|\s*(.+)$/i);
	if (cron?.[1] && cron[2]) {
		return {
			action: "create",
			cronExpression: cron[1].trim(),
			description: cron[2].trim(),
			message: cron[2].trim(),
		};
	}

	throw new Error("Invalid reminder command");
}

async function executeRemind(
	params: RemindParams,
	ctx: ToolContext,
): Promise<ToolResult> {
	switch (params.action) {
		case "create":
			return handleCreate(params, ctx);
		case "list":
			return handleList(ctx);
		case "cancel":
			return handleCancel(params, ctx);
		default:
			return { text: "Unknown action.", error: "Unknown action" };
	}
}

async function handleCreate(
	params: RemindParams,
	ctx: ToolContext,
): Promise<ToolResult> {
	if (!ctx.canManageReminders) {
		return {
			text: "Only chat admins can create reminders in this chat.",
			error: "Unauthorized",
		};
	}

	if (!params.description) {
		return {
			text: "Description is required for creating a reminder.",
			error: "Missing description",
		};
	}

	if (!params.fireAt && !params.cronExpression) {
		return {
			text: "Either fireAt (one-time) or cronExpression (recurring) is required.",
			error: "Missing schedule",
		};
	}

	// Check abuse limits
	const activeCount = await countActiveReminders(
		ctx.db,
		ctx.user.id,
		ctx.chat.id,
	);
	if (activeCount >= MAX_ACTIVE_PER_CHAT) {
		return {
			text: `Maximum ${MAX_ACTIVE_PER_CHAT} active reminders per chat reached. Cancel some first.`,
			error: "Limit reached",
		};
	}

	const isRecurring = !!params.cronExpression;
	const cronExpression = params.cronExpression ?? null;

	if (isRecurring) {
		const recurringCount = await countRecurringReminders(ctx.db, ctx.user.id);
		if (recurringCount >= MAX_RECURRING_PER_USER) {
			return {
				text: `Maximum ${MAX_RECURRING_PER_USER} recurring reminders per user reached.`,
				error: "Recurring limit reached",
			};
		}

		// Validate cron expression (enforces min 1h interval)
		if (!cronExpression) {
			return { text: "cronExpression is required.", error: "Missing cron" };
		}
		const cronError = validateCron(cronExpression);
		if (cronError) {
			return { text: cronError, error: cronError };
		}
	}

	const usesLlm = !!params.prompt;
	if (usesLlm && isRecurring) {
		return {
			text: "Recurring LLM reminders are disabled. Use a plain recurring reminder or a one-time LLM reminder.",
			error: "Recurring LLM reminders disabled",
		};
	}

	let fireAt = params.fireAt ? new Date(params.fireAt) : null;

	if (params.fireAt && (!fireAt || Number.isNaN(fireAt.getTime()))) {
		return { text: "Invalid fireAt datetime.", error: "Invalid datetime" };
	}

	if (isRecurring && cronExpression) {
		fireAt = parseCronToNextDate(cronExpression);
		if (!fireAt) {
			return {
				text: "Could not compute the next reminder time from that cron expression.",
				error: "Invalid cron schedule",
			};
		}
	}

	// Validate fire time is in the future
	if (fireAt && fireAt <= new Date()) {
		return { text: "Reminder time must be in the future.", error: "Past time" };
	}

	const metadata = getReminderMetadata(ctx);
	await createReminder(ctx.db, {
		chatId: ctx.chat.id,
		userId: ctx.user.id,
		threadId: metadata.threadId,
		description: params.description,
		message: params.message ?? (usesLlm ? null : params.description),
		prompt: params.prompt ?? null,
		usesLlm,
		fireAt,
		cronExpression,
		isRecurring,
		replyToMessageId: metadata.replyToMessageId,
	});

	const typeLabel = isRecurring ? "recurring" : "one-time";
	const modeLabel = usesLlm ? "LLM" : "plain";
	const timeLabel = fireAt
		? isRecurring
			? `cron: ${cronExpression} (next: ${formatDate(fireAt)})`
			: formatDate(fireAt)
		: `cron: ${cronExpression}`;

	return {
		text: `Reminder created (${typeLabel}, ${modeLabel}): "${params.description}" — ${timeLabel}`,
	};
}

async function handleList(ctx: ToolContext): Promise<ToolResult> {
	const reminders = await getRemindersForChat(ctx.db, ctx.chat.id);

	if (reminders.length === 0) {
		return { text: "No active reminders in this chat." };
	}

	const lines = reminders.map((r, i) => {
		const schedule = r.isRecurring
			? `cron: ${r.cronExpression}`
			: r.fireAt
				? formatDate(r.fireAt)
				: "—";
		return `${i + 1}. [${r.id.slice(0, 8)}] ${r.description} — ${schedule}`;
	});

	return { text: `Active reminders:\n${lines.join("\n")}` };
}

async function handleCancel(
	params: RemindParams,
	ctx: ToolContext,
): Promise<ToolResult> {
	if (!params.reminderId) {
		return {
			text: "reminderId is required for cancel action.",
			error: "Missing ID",
		};
	}

	const reminder = await getReminderById(ctx.db, params.reminderId);
	if (!reminder) {
		return { text: "Reminder not found.", error: "Not found" };
	}

	if (reminder.chatId !== ctx.chat.id) {
		return { text: "Reminder not found in this chat.", error: "Not found" };
	}

	// Only creator or chat admin can cancel
	if (reminder.userId !== ctx.user.id && !ctx.isChatAdmin) {
		return {
			text: "Only the reminder creator or a chat admin can cancel it.",
			error: "Unauthorized",
		};
	}

	await cancelReminder(ctx.db, params.reminderId);
	return { text: `Reminder "${reminder.description}" cancelled.` };
}

export const remindTool: ToolDefinition<RemindParams> = {
	name: "remind",
	commands: ["/remind", "/r"],
	description:
		"Create, list, or cancel reminders. Supports one-time and recurring (cron) schedules, plain text or LLM-generated messages.",
	helpText: "tool-remind",
	category: "utility",
	parameters: remindParamsSchema,
	parseCommand: parseRemindCommand,
	usage:
		"/remind list | /remind cancel <id> | /remind at <ISO datetime> <message> | /remind cron <cron> | <message>",
	execute: executeRemind,
	credits: 0,
	freeDaily: 5,
};
