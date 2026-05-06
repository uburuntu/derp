/** Admin notification utility — sends events to the admin events chat */

import type { Api } from "grammy";
import { config } from "../config";
import { logger } from "./observability";
import { escapeHtml } from "./sanitize";

let botApi: Api | null = null;

/** Initialize the notifier with the bot API instance */
export function initAdminNotify(api: Api): void {
    botApi = api;
}

export interface AdminNotifyResult {
    delivered: boolean;
    error?: string;
}

/** Validate that critical admin notifications can be delivered. */
export async function assertAdminNotifyReady(): Promise<void> {
    const chatId = config.botAdminEventsChatId;
    if (!chatId) throw new Error("BOT_ADMIN_EVENTS_CHAT_ID is required");
    if (!botApi) throw new Error("Admin notifier is not initialized");
    await botApi.getChat(chatId);
}

/** Send a notification to the admin events chat (no-op if not configured). */
export async function notifyAdmins(
    message: string,
    options: { critical?: boolean } = {},
): Promise<AdminNotifyResult> {
    const chatId = config.botAdminEventsChatId;
    if (!chatId || !botApi) {
        const error = "admin notification chat is not configured";
        if (options.critical) throw new Error(error);
        return { delivered: false, error };
    }

    try {
        await botApi.sendMessage(chatId, message, { parse_mode: "HTML" });
        return { delivered: true };
    } catch (err) {
        const error = err instanceof Error ? err.message : String(err);
        logger.error("admin_notify_failed", {
            error,
        });
        if (options.critical) throw new Error(error);
        return { delivered: false, error };
    }
}

/** Format a payment notification */
export function formatPaymentNotification(params: {
    type: "subscription" | "purchase" | "donation";
    userId: number;
    username?: string | null;
    firstName: string | null;
    planOrPack: string;
    stars: number;
    credits: number;
    chargeId: string;
    providerChargeId?: string;
    chatId?: number;
    isRenewal?: boolean;
}): string {
    const displayName = params.firstName ?? "user";
    const userLink = params.username
        ? `@${escapeHtml(params.username)}`
        : `<a href="tg://user?id=${params.userId}">${escapeHtml(displayName)}</a>`;

    const label = params.isRenewal ? "RENEWAL" : params.type.toUpperCase();
    const chatLine = params.chatId
        ? `\nChat: <code>${params.chatId}</code>`
        : "";
    const chargeId = escapeHtml(params.chargeId);
    const amountLine =
        params.type === "donation"
            ? `Amount: ${params.stars}⭐ donation${chatLine}\n`
            : `Amount: ${params.stars}⭐ → ${params.credits} credits${chatLine}\n`;

    return (
        `<b>${label}</b>\n` +
        `User: ${userLink} (<code>${params.userId}</code>)\n` +
        `Plan: ${escapeHtml(params.planOrPack)}\n` +
        amountLine +
        `Charge: <code>${chargeId}</code>\n` +
        `\nRefund: <code>/refund ${params.userId} ${chargeId}</code>`
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
    const status = params.success
        ? "SUCCESS"
        : `FAILED: ${escapeHtml(params.error ?? "unknown")}`;
    return (
        `<b>REFUND ${status}</b>\n` +
        `Admin: <code>${params.adminId}</code>\n` +
        `User: <code>${params.targetUserId}</code>\n` +
        `Charge: <code>${escapeHtml(params.chargeId)}</code>`
    );
}
