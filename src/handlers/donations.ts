/** Donations handler — lightweight Telegram Stars support flow. */

import { Composer, InlineKeyboard } from "grammy";
import type { DerpContext } from "../bot/context";
import {
	formatPaymentNotification,
	notifyAdmins,
} from "../common/admin-notify";
import { derpMetrics } from "../common/observability";
import { MESSAGE_EFFECTS } from "../common/telegram";
import { recordDonationPayment } from "../db/queries/credits";

const donationsComposer = new Composer<DerpContext>();
const DONATION_OPTIONS = [20, 100, 250];

function parseDonationAmount(input: string | undefined): number | null {
	const amount = Number.parseInt((input ?? "").trim(), 10);
	if (Number.isNaN(amount) || amount <= 0) return null;
	return Math.min(amount, 2500);
}

function donationPayload(amount: number): string {
	return `donate:${amount}`;
}

function amountFromPayload(payload: string): number | null {
	const [kind, rawAmount] = payload.split(":");
	if (kind !== "donate" || !rawAmount) return null;
	return parseDonationAmount(rawAmount);
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
		donationPayload(amount),
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
		donationPayload(amount),
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
	const isDonation = query.invoice_payload.startsWith("donate:");
	const amount = amountFromPayload(query.invoice_payload);
	if (amount == null) {
		if (isDonation) {
			await ctx.answerPreCheckoutQuery(false, ctx.t("donate-invalid"));
			return;
		}
		return next();
	}

	if (query.currency !== "XTR" || query.total_amount !== amount) {
		await ctx.answerPreCheckoutQuery(false, ctx.t("donate-invalid"));
		return;
	}

	await ctx.answerPreCheckoutQuery(true);
});

donationsComposer.on("message:successful_payment", async (ctx, next) => {
	const payment = ctx.message?.successful_payment;
	if (!payment) return;

	const amount = amountFromPayload(payment.invoice_payload);
	if (amount == null) return next();
	if (!ctx.dbUser) return;

	const userId = ctx.from?.id ?? ctx.dbUser?.telegramId ?? 0;
	const result = await recordDonationPayment(ctx.db, {
		userId: ctx.dbUser.id,
		chatId: ctx.dbChat?.id ?? null,
		telegramChargeId: payment.telegram_payment_charge_id,
		providerChargeId: payment.provider_payment_charge_id,
		invoicePayload: payment.invoice_payload,
		currency: payment.currency,
		stars: amount,
		productType: "donation",
		productId: "support",
		creditTarget: "none",
		credits: 0,
		meta: {
			chatTelegramId: ctx.chat?.id,
			threadId: ctx.message?.message_thread_id,
		},
	});
	if (!result.applied) return;

	try {
		await ctx.reply(ctx.t("donate-thanks", { stars: amount }), {
			parse_mode: "HTML",
			message_effect_id: MESSAGE_EFFECTS.party,
			...threadOptions(ctx),
		});
	} catch {
		await ctx.reply(ctx.t("donate-thanks", { stars: amount }), {
			parse_mode: "HTML",
			...threadOptions(ctx),
		});
	}

	await notifyAdmins(
		formatPaymentNotification({
			type: "donation",
			userId,
			username: ctx.from?.username ?? ctx.dbUser?.username ?? null,
			firstName: ctx.from?.first_name ?? ctx.dbUser?.firstName ?? null,
			planOrPack: "Donation",
			stars: amount,
			credits: 0,
			chargeId: payment.telegram_payment_charge_id,
			chatId: ctx.chat?.id,
		}),
	);

	derpMetrics.creditRevenue.add(amount, { source: "donation" });
	derpMetrics.creditTransactions.add(1, { type: "donation" });
});

export { donationsComposer };
