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
import { validateCron } from "../scheduler/cron";
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

	if (isRecurring) {
		const recurringCount = await countRecurringReminders(ctx.db, ctx.user.id);
		if (recurringCount >= MAX_RECURRING_PER_USER) {
			return {
				text: `Maximum ${MAX_RECURRING_PER_USER} recurring reminders per user reached.`,
				error: "Recurring limit reached",
			};
		}

		// Validate cron expression (enforces min 1h interval)
		const cronError = validateCron(params.cronExpression!);
		if (cronError) {
			return { text: cronError, error: cronError };
		}
	}

	const usesLlm = !!params.prompt;
	const fireAt = params.fireAt ? new Date(params.fireAt) : null;

	// Validate fire time is in the future
	if (fireAt && fireAt <= new Date()) {
		return { text: "Reminder time must be in the future.", error: "Past time" };
	}

	await createReminder(ctx.db, {
		chatId: ctx.chat.id,
		userId: ctx.user.id,
		description: params.description,
		message: params.message ?? null,
		prompt: params.prompt ?? null,
		usesLlm,
		fireAt,
		cronExpression: params.cronExpression ?? null,
		isRecurring,
	});

	const typeLabel = isRecurring ? "recurring" : "one-time";
	const modeLabel = usesLlm ? "LLM" : "plain";
	const timeLabel = fireAt
		? formatDate(fireAt)
		: `cron: ${params.cronExpression}`;

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

	// Only creator or chat admin can cancel
	if (reminder.userId !== ctx.user.id) {
		return {
			text: "Only the reminder creator can cancel it.",
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
	execute: executeRemind,
	credits: 0,
	freeDaily: 5,
};
