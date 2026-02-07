/** Admin notification utility — sends events to the admin events chat */

import type { Api } from "grammy";
import { config } from "../config";
import { logger } from "./observability";

let botApi: Api | null = null;

/** Initialize the notifier with the bot API instance */
export function initAdminNotify(api: Api): void {
	botApi = api;
}

/** Send a notification to the admin events chat (no-op if not configured) */
export async function notifyAdmins(message: string): Promise<void> {
	const chatId = config.botAdminEventsChatId;
	if (!chatId || !botApi) return;

	try {
		await botApi.sendMessage(chatId, message, { parse_mode: "HTML" });
	} catch (err) {
		logger.error("admin_notify_failed", {
			error: err instanceof Error ? err.message : String(err),
		});
	}
}

/** Format a payment notification */
export function formatPaymentNotification(params: {
	type: "subscription" | "purchase";
	userId: number;
	username?: string | null;
	firstName: string;
	planOrPack: string;
	stars: number;
	credits: number;
	chargeId: string;
	providerChargeId?: string;
	chatId?: number;
	isRenewal?: boolean;
}): string {
	const userLink = params.username
		? `@${params.username}`
		: `<a href="tg://user?id=${params.userId}">${params.firstName}</a>`;

	const label = params.isRenewal ? "RENEWAL" : params.type.toUpperCase();
	const chatLine = params.chatId ? `\nChat: <code>${params.chatId}</code>` : "";

	return (
		`<b>${label}</b>\n` +
		`User: ${userLink} (<code>${params.userId}</code>)\n` +
		`Plan: ${params.planOrPack}\n` +
		`Amount: ${params.stars}⭐ → ${params.credits} credits${chatLine}\n` +
		`Charge: <code>${params.chargeId}</code>\n` +
		`\nRefund: <code>/refund ${params.userId} ${params.chargeId}</code>`
	);
}

/** Format a refund notification */
export function formatRefundNotification(params: {
	adminId: number;
	targetUserId: number;
	chargeId: string;
	success: boolean;
	error?: string;
}): string {
	const status = params.success ? "SUCCESS" : `FAILED: ${params.error}`;
	return (
		`<b>REFUND ${status}</b>\n` +
		`Admin: <code>${params.adminId}</code>\n` +
		`User: <code>${params.targetUserId}</code>\n` +
		`Charge: <code>${params.chargeId}</code>`
	);
}
