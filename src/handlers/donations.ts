/** Donations handler — lightweight Telegram Stars support flow. */

import { Composer, InlineKeyboard } from "grammy";
import type { SuccessfulPayment } from "grammy/types";
import type { DerpContext } from "../bot/context";
import {
	formatPaymentNotification,
	notifyAdmins,
} from "../common/admin-notify";
import {
	derpMetrics,
	logger,
	recordHandledFailure,
} from "../common/observability";
import { escapeHtml } from "../common/sanitize";
import { MESSAGE_EFFECTS } from "../common/telegram";
import {
	buildDonationPayload,
	parseDonationPayload,
	type SignedDonationPayload,
} from "../credits/payment-payload";
import { getChatByTelegramId } from "../db/queries/chats";
import {
	markPaymentSettlementFailed,
	recordDonationPayment,
} from "../db/queries/credits";

const donationsComposer = new Composer<DerpContext>();
const DONATION_OPTIONS = [20, 100, 250];

function parseDonationAmount(input: string | undefined): number | null {
	const amount = Number.parseInt((input ?? "").trim(), 10);
	if (Number.isNaN(amount) || amount <= 0) return null;
	return Math.min(amount, 2500);
}

function donationPayload(
	amount: number,
	targetChatId: number,
	targetThreadId: number | null,
): string {
	return buildDonationPayload({ amount, targetChatId, targetThreadId });
}

function threadOptions(ctx: DerpContext) {
	return {
		message_thread_id: ctx.message?.message_thread_id,
		reply_to_message_id: ctx.message?.message_id,
	};
}

function callbackThreadId(ctx: DerpContext): number | undefined {
	const message = ctx.callbackQuery?.message;
	if (!message || !("message_id" in message)) return undefined;
	return (message as { message_thread_id?: number }).message_thread_id;
}

async function sendDonationInvoice(
	ctx: DerpContext,
	amount: number,
): Promise<void> {
	if (!ctx.chat) return;

	await ctx.api.sendInvoice(
		ctx.chat.id,
		"Support Derp",
		`${amount} Stars to support Derp development and hosting`,
		donationPayload(
			amount,
			ctx.chat.id,
			ctx.message?.message_thread_id ?? null,
		),
		"XTR",
		[{ label: "Donation", amount }],
		{
			provider_token: "",
			message_thread_id: ctx.message?.message_thread_id,
		},
	);
}

donationsComposer.command(["donate", "support"], async (ctx) => {
	const explicitAmount = parseDonationAmount(ctx.match);
	if (explicitAmount) {
		await sendDonationInvoice(ctx, explicitAmount);
		return;
	}

	const keyboard = new InlineKeyboard();
	for (const amount of DONATION_OPTIONS) {
		keyboard.text(`${amount}⭐`, `donate:${amount}`).row();
	}

	await ctx.reply(ctx.t("donate-choose"), {
		parse_mode: "HTML",
		reply_markup: keyboard,
		...threadOptions(ctx),
	});
});

donationsComposer.callbackQuery(/^donate:(\d+)$/, async (ctx) => {
	const amount = parseDonationAmount(ctx.match[1]);
	if (!amount) {
		await ctx.answerCallbackQuery(ctx.t("donate-invalid"));
		return;
	}
	if (!ctx.chat) return;

	await ctx.answerCallbackQuery();
	await ctx.api.sendInvoice(
		ctx.chat.id,
		"Support Derp",
		`${amount} Stars to support Derp development and hosting`,
		donationPayload(amount, ctx.chat.id, callbackThreadId(ctx) ?? null),
		"XTR",
		[{ label: "Donation", amount }],
		{
			provider_token: "",
			message_thread_id: callbackThreadId(ctx),
		},
	);
});

donationsComposer.on("pre_checkout_query", async (ctx, next) => {
	const query = ctx.preCheckoutQuery;
	const payload = parseDonationPayload(query.invoice_payload);
	if (!payload) {
		return next();
	}

	if (query.currency !== "XTR" || query.total_amount !== payload.amount) {
		await ctx.answerPreCheckoutQuery(false, ctx.t("donate-invalid"));
		return;
	}

	await ctx.answerPreCheckoutQuery(true);
});

donationsComposer.on("message:successful_payment", async (ctx, next) => {
	const payment = ctx.message?.successful_payment;
	if (!payment) return;

	const payload = parseDonationPayload(payment.invoice_payload);
	if (!payload) return next();
	if (!ctx.dbUser) return;

	const userId = ctx.from?.id ?? ctx.dbUser?.telegramId ?? 0;
	const targetChat = await getChatByTelegramId(ctx.db, payload.targetChatId);
	const result = await applyDonationOrReport(ctx, payment, payload, () => {
		return recordDonationPayment(ctx.db, {
			userId: ctx.dbUser.id,
			chatId: targetChat?.id ?? null,
			telegramChargeId: payment.telegram_payment_charge_id,
			providerChargeId: payment.provider_payment_charge_id,
			invoicePayload: payment.invoice_payload,
			currency: payment.currency,
			stars: payload.amount,
			productType: "donation",
			productId: "support",
			creditTarget: "none",
			credits: 0,
			meta: {
				chatTelegramId: payload.targetChatId,
				threadId: payload.targetThreadId,
				targetChatMissing: targetChat ? undefined : true,
			},
		});
	});
	if (!result.applied) return;

	await runDonationSideEffects(ctx, payment, payload, {
		replyText: ctx.t("donate-thanks", { stars: payload.amount }),
		adminText: formatPaymentNotification({
			type: "donation",
			userId,
			username: ctx.from?.username ?? ctx.dbUser?.username ?? null,
			firstName: ctx.from?.first_name ?? ctx.dbUser?.firstName ?? null,
			planOrPack: "Donation",
			stars: payload.amount,
			credits: 0,
			chargeId: payment.telegram_payment_charge_id,
			chatId: payload.targetChatId,
		}),
	});

	derpMetrics.creditRevenue.add(payload.amount, { source: "donation" });
	derpMetrics.creditTransactions.add(1, { type: "donation" });
});

async function applyDonationOrReport<T extends { applied: boolean }>(
	ctx: DerpContext,
	payment: SuccessfulPayment,
	payload: SignedDonationPayload,
	applyPayment: () => Promise<T>,
): Promise<T> {
	try {
		return await applyPayment();
	} catch (err) {
		const reason = err instanceof Error ? err.message : String(err);
		recordHandledFailure("donation", reason, {
			reason_code: "db_apply",
		});
		logger.error("donation_apply_failed", {
			userId: ctx.dbUser?.telegramId,
			targetChatId: payload.targetChatId,
			chargeId: payment.telegram_payment_charge_id,
			payload: payment.invoice_payload,
			error: reason,
		});
		await ctx.reply(
			`⚠️ <b>Donation received</b>\n\nI could not record it automatically. Charge: <code>${escapeHtml(payment.telegram_payment_charge_id)}</code>.`,
			{
				parse_mode: "HTML",
				...threadOptions(ctx),
			},
		);
		await markPaymentSettlementFailed(
			ctx.db,
			payment.telegram_payment_charge_id,
			reason,
		).catch((markErr) => {
			logger.error("donation_mark_settlement_failed_failed", {
				chargeId: payment.telegram_payment_charge_id,
				error: markErr instanceof Error ? markErr.message : String(markErr),
			});
		});
		await notifyAdmins(
			`⚠️ <b>Donation processing failed</b>\n\nUser: <code>${ctx.dbUser?.telegramId ?? "unknown"}</code>\nTarget chat: <code>${payload.targetChatId}</code>\nCharge: <code>${escapeHtml(payment.telegram_payment_charge_id)}</code>\nPayload: <code>${escapeHtml(payment.invoice_payload)}</code>\nAmount: ${payment.total_amount} ${escapeHtml(payment.currency)}\nReason: ${escapeHtml(reason)}`,
			{ critical: true },
		);
		return { applied: false } as T;
	}
}

async function runDonationSideEffects(
	ctx: DerpContext,
	payment: SuccessfulPayment,
	payload: SignedDonationPayload,
	effects: { replyText: string; adminText: string },
): Promise<void> {
	const failures: string[] = [];
	try {
		await ctx.api.sendMessage(payload.targetChatId, effects.replyText, {
			parse_mode: "HTML",
			message_thread_id: payload.targetThreadId ?? undefined,
			message_effect_id: MESSAGE_EFFECTS.party,
		});
	} catch (err) {
		failures.push(`reply: ${err instanceof Error ? err.message : String(err)}`);
		try {
			await ctx.api.sendMessage(payload.targetChatId, effects.replyText, {
				parse_mode: "HTML",
				message_thread_id: payload.targetThreadId ?? undefined,
			});
		} catch (fallbackErr) {
			failures.push(
				`reply_fallback: ${fallbackErr instanceof Error ? fallbackErr.message : String(fallbackErr)}`,
			);
		}
	}

	await notifyAdmins(effects.adminText).catch((err) => {
		failures.push(`admin: ${err instanceof Error ? err.message : String(err)}`);
	});
	if (failures.length === 0) return;

	const reason = failures.join("; ");
	recordHandledFailure("donation_notification", reason, {
		reason_code: "post_commit",
	});
	logger.error("donation_post_commit_side_effect_failed", {
		chargeId: payment.telegram_payment_charge_id,
		failures,
	});
}

export { donationsComposer };
