/** Credits handler — /credits, /buy, payment flows */

import { Composer, type NextFunction } from "grammy";
import type { DerpContext } from "../bot/context";
import {
	formatPaymentNotification,
	notifyAdmins,
} from "../common/admin-notify";
import { derpMetrics } from "../common/observability";
import { escapeHtml } from "../common/sanitize";
import { MESSAGE_EFFECTS } from "../common/telegram";
import { getTopUpPack } from "../credits/packs";
import { getSubscriptionPlan } from "../credits/subscriptions";
import { buildBuyKeyboard, formatBalanceMessage } from "../credits/ui";
import {
	addChatCreditsWithResult,
	addUserCreditsWithResult,
	applySubscriptionPayment,
	getBalances,
	reconcileStarRefund,
	transferUserCreditsToChat,
} from "../db/queries/credits";

const creditsComposer = new Composer<DerpContext>();

type PaymentPayload =
	| { type: "sub"; planId: string }
	| { type: "pack"; packId: string; target: "user" | "chat" };

const TRANSFER_PROMPT_MARKERS = [
	"Move personal credits",
	"Перенеси личные кредиты",
	"Transfer credits",
];

function parsePaymentPayload(payload: string): PaymentPayload | null {
	const parts = payload.split(":");
	if (parts[0] === "sub" && parts[1] && parts.length === 2) {
		return { type: "sub", planId: parts[1] };
	}
	if (
		parts[0] === "pack" &&
		parts[1] &&
		(parts[2] === "user" || parts[2] === "chat") &&
		parts.length === 3
	) {
		return { type: "pack", packId: parts[1], target: parts[2] };
	}
	return null;
}

function expectedStars(payload: PaymentPayload): number | null {
	if (payload.type === "sub") {
		return getSubscriptionPlan(payload.planId)?.stars ?? null;
	}
	return getTopUpPack(payload.packId)?.stars ?? null;
}

function validateStarsPayment(
	payloadText: string,
	currency: string,
	totalAmount: number,
): { payload: PaymentPayload; stars: number } | { error: string } {
	const payload = parsePaymentPayload(payloadText);
	if (!payload) return { error: "Unknown invoice payload" };
	if (currency !== "XTR") return { error: "Unsupported payment currency" };

	const stars = expectedStars(payload);
	if (stars == null) return { error: "Unknown plan or pack" };
	if (totalAmount !== stars) return { error: "Invoice amount mismatch" };

	return { payload, stars };
}

type SuccessfulSubscriptionPaymentFields = {
	is_recurring?: boolean;
	is_first_recurring?: boolean;
	subscription_expiration_date?: number;
};

function getSubscriptionPaymentFields(
	payment: unknown,
): SuccessfulSubscriptionPaymentFields {
	return payment as SuccessfulSubscriptionPaymentFields;
}

function getSubscriptionExpiry(
	payment: unknown,
	currentExpiry: Date | null,
): Date {
	const fields = getSubscriptionPaymentFields(payment);
	if (
		typeof fields.subscription_expiration_date === "number" &&
		fields.subscription_expiration_date > 0
	) {
		return new Date(fields.subscription_expiration_date * 1000);
	}

	const now = new Date();
	const thirtyDays = 30 * 24 * 60 * 60 * 1000;
	const base = currentExpiry && currentExpiry > now ? currentExpiry : now;
	return new Date(base.getTime() + thirtyDays);
}

function isSubscriptionRenewal(payment: unknown): boolean {
	const fields = getSubscriptionPaymentFields(payment);
	return fields.is_recurring === true && fields.is_first_recurring !== true;
}

async function replyWithPaymentEffect(
	ctx: DerpContext,
	text: string,
): Promise<void> {
	try {
		await ctx.reply(text, {
			parse_mode: "HTML",
			message_effect_id: MESSAGE_EFFECTS.party,
		});
	} catch {
		await ctx.reply(text, { parse_mode: "HTML" });
	}
}

// ── /credits, /balance, /bal ────────────────────────────────────────────────

creditsComposer.command(["credits", "balance", "bal"], async (ctx) => {
	if (!ctx.dbUser || !ctx.dbChat) return;

	const { userCredits, chatCredits } = await getBalances(
		ctx.db,
		ctx.dbUser.telegramId,
		ctx.dbChat.telegramId,
	);

	const message = formatBalanceMessage(
		userCredits,
		chatCredits,
		ctx.dbUser.subscriptionTier,
		ctx.dbUser.subscriptionExpiresAt,
		(key, args) => ctx.t(key, args),
	);

	await ctx.reply(message, {
		parse_mode: "HTML",
		reply_to_message_id: ctx.message?.message_id,
	});
});

// ── /buy ────────────────────────────────────────────────────────────────────

creditsComposer.command(["buy", "purchase", "shop"], async (ctx) => {
	const isGroup = ctx.chat?.type === "group" || ctx.chat?.type === "supergroup";
	const keyboard = buildBuyKeyboard(isGroup, (key, args) => ctx.t(key, args));

	await ctx.reply(ctx.t("buy-choose"), {
		parse_mode: "HTML",
		reply_markup: keyboard,
		reply_to_message_id: ctx.message?.message_id,
	});
});

creditsComposer.command(["buy_chat", "buychat"], async (ctx) => {
	const isGroup = ctx.chat?.type === "group" || ctx.chat?.type === "supergroup";
	if (!isGroup) {
		await ctx.reply(ctx.t("buy-chat-groups-only"), {
			parse_mode: "HTML",
			reply_to_message_id: ctx.message?.message_id,
		});
		return;
	}

	const keyboard = buildBuyKeyboard(true, (key, args) => ctx.t(key, args));
	await ctx.reply(ctx.t("buy-choose"), {
		parse_mode: "HTML",
		reply_markup: keyboard,
		reply_to_message_id: ctx.message?.message_id,
	});
});

// ── Callback: subscription selection ────────────────────────────────────────

creditsComposer.callbackQuery(/^sub:(.+)$/, async (ctx) => {
	const planId = ctx.match[1];
	if (!planId) return;
	const plan = getSubscriptionPlan(planId);
	if (!plan) {
		await ctx.answerCallbackQuery(ctx.t("buy-plan-not-found"));
		return;
	}

	// Create subscription invoice link
	const link = await ctx.api.createInvoiceLink(
		`${plan.label} Subscription`,
		`${plan.credits} credits/month (${plan.savings} savings)`,
		`sub:${plan.id}`,
		"", // empty provider_token for Stars
		"XTR",
		[{ label: `${plan.label} Subscription`, amount: plan.stars }],
		{ subscription_period: 2592000 },
	);

	await ctx.answerCallbackQuery();
	await ctx.reply(ctx.t("buy-subscribe", { plan: escapeHtml(plan.label) }), {
		parse_mode: "HTML",
		reply_markup: {
			inline_keyboard: [
				[{ text: ctx.t("buy-pay-button", { stars: plan.stars }), url: link }],
			],
		},
	});
});

// ── Callback: top-up pack selection (personal) ──────────────────────────────

creditsComposer.callbackQuery(/^pack:(.+)$/, async (ctx) => {
	const packId = ctx.match[1];
	if (!packId) return;
	const pack = getTopUpPack(packId);
	if (!pack) {
		await ctx.answerCallbackQuery(ctx.t("buy-pack-not-found"));
		return;
	}
	if (!ctx.chat) return;

	await ctx.answerCallbackQuery();
	await ctx.api.sendInvoice(
		ctx.chat.id,
		`${pack.label} Credit Pack`,
		`${pack.credits} credits`,
		`pack:${pack.id}:user`,
		"XTR",
		[{ label: `${pack.label} Pack`, amount: pack.stars }],
		{ provider_token: "" },
	);
});

// ── Callback: group credit pack ─────────────────────────────────────────────

creditsComposer.callbackQuery(/^group_pack:(.+)$/, async (ctx) => {
	const packId = ctx.match[1];
	if (!packId) return;
	const pack = getTopUpPack(packId);
	if (!pack) {
		await ctx.answerCallbackQuery(ctx.t("buy-pack-not-found"));
		return;
	}
	if (!ctx.chat) return;

	await ctx.answerCallbackQuery();
	await ctx.api.sendInvoice(
		ctx.chat.id,
		`${pack.label} Group Credit Pack`,
		`${pack.credits} credits for this chat`,
		`pack:${pack.id}:chat`,
		"XTR",
		[{ label: `${pack.label} Group Pack`, amount: pack.stars }],
		{ provider_token: "" },
	);
});

// ── Callback: credit transfer (personal → group pool) ──────────────────────

creditsComposer.callbackQuery("transfer", async (ctx) => {
	if (!ctx.dbUser || !ctx.dbChat) return;

	const isGroup = ctx.chat?.type === "group" || ctx.chat?.type === "supergroup";
	if (!isGroup) {
		await ctx.answerCallbackQuery(ctx.t("transfer-groups-only"));
		return;
	}

	const { userCredits } = await getBalances(
		ctx.db,
		ctx.dbUser.telegramId,
		ctx.dbChat.telegramId,
	);

	if (userCredits < 100) {
		await ctx.answerCallbackQuery(ctx.t("transfer-min"));
		return;
	}

	await ctx.answerCallbackQuery();
	await ctx.reply(ctx.t("transfer-prompt", { balance: userCredits }), {
		parse_mode: "HTML",
		reply_markup: { force_reply: true, selective: true },
	});
});

// Handle transfer amount as a reply
creditsComposer.hears(/^\d+$/, async (ctx, next: NextFunction) => {
	// Only handle if replying to a transfer prompt from the bot
	if (!ctx.dbUser || !ctx.dbChat) return next();
	if (!ctx.message?.reply_to_message?.from?.is_bot) return next();
	const replyText = ctx.message.reply_to_message.text ?? "";
	if (!TRANSFER_PROMPT_MARKERS.some((marker) => replyText.includes(marker))) {
		return next();
	}

	const text = ctx.message.text;
	if (!text) return;
	const amount = Number.parseInt(text, 10);
	if (Number.isNaN(amount) || amount < 100) {
		await ctx.reply(ctx.t("transfer-min"), { parse_mode: "HTML" });
		return;
	}

	const { userCredits } = await getBalances(
		ctx.db,
		ctx.dbUser.telegramId,
		ctx.dbChat.telegramId,
	);

	if (userCredits < amount) {
		await ctx.reply(ctx.t("transfer-insufficient", { balance: userCredits }), {
			parse_mode: "HTML",
		});
		return;
	}

	try {
		const result = await transferUserCreditsToChat(
			ctx.db,
			ctx.dbUser.id,
			ctx.dbChat.id,
			amount,
			`transfer:${ctx.dbUser.id}:${ctx.dbChat.id}:${ctx.message.message_id}`,
			{ telegramMessageId: ctx.message.message_id },
		);
		if (!result.applied) {
			await ctx.reply(ctx.t("transfer-already-processed"), {
				parse_mode: "HTML",
			});
			return;
		}
		await ctx.reply(ctx.t("transfer-success", { amount }), {
			parse_mode: "HTML",
		});
	} catch (err) {
		const msg = err instanceof Error ? err.message : String(err);
		await ctx.reply(ctx.t("transfer-failed", { error: escapeHtml(msg) }), {
			parse_mode: "HTML",
		});
	}
});

// ── Pre-checkout query ──────────────────────────────────────────────────────

creditsComposer.on("pre_checkout_query", async (ctx) => {
	const query = ctx.preCheckoutQuery;
	const validation = validateStarsPayment(
		query.invoice_payload,
		query.currency,
		query.total_amount,
	);
	if ("error" in validation) {
		await ctx.answerPreCheckoutQuery(false, validation.error);
		return;
	}

	await ctx.answerPreCheckoutQuery(true);
});

// ── Successful payment ──────────────────────────────────────────────────────

creditsComposer.on("message:successful_payment", async (ctx) => {
	if (!ctx.dbUser || !ctx.dbChat) return;

	const payment = ctx.message?.successful_payment;
	if (!payment) return;
	const validation = validateStarsPayment(
		payment.invoice_payload,
		payment.currency,
		payment.total_amount,
	);
	if ("error" in validation) {
		await ctx.reply(`Payment rejected: ${escapeHtml(validation.error)}`, {
			parse_mode: "HTML",
		});
		await notifyAdmins(
			`⚠️ <b>Payment validation failed</b>\n\nUser: <code>${ctx.dbUser.telegramId}</code>\nPayload: <code>${escapeHtml(payment.invoice_payload)}</code>\nReason: ${escapeHtml(validation.error)}`,
		);
		return;
	}

	const { payload } = validation;
	const chargeId = payment.telegram_payment_charge_id;

	if (payload.type === "sub") {
		// Subscription payment
		const plan = getSubscriptionPlan(payload.planId);
		if (!plan) return;

		const subscriptionFields = getSubscriptionPaymentFields(payment);
		const newExpiry = getSubscriptionExpiry(
			payment,
			ctx.dbUser.subscriptionExpiresAt,
		);
		const isRenewal = isSubscriptionRenewal(payment);

		const result = await applySubscriptionPayment(
			ctx.db,
			ctx.dbUser.id,
			plan.credits,
			plan.id,
			chargeId,
			newExpiry,
			{
				planId: plan.id,
				stars: plan.stars,
				isRenewal,
				isRecurring: subscriptionFields.is_recurring === true,
				isFirstRecurring: subscriptionFields.is_first_recurring === true,
				telegramSubscriptionExpirationDate:
					subscriptionFields.subscription_expiration_date,
			},
		);
		if (!result.applied) return;

		const msg = isRenewal
			? `${plan.label} subscription renewed! ${plan.credits} credits added.`
			: `Subscribed to ${plan.label}! ${plan.credits} credits added. Your subscription renews monthly.`;
		await replyWithPaymentEffect(ctx, msg);

		await notifyAdmins(
			formatPaymentNotification({
				type: "subscription",
				userId: ctx.dbUser.telegramId,
				username: ctx.dbUser.username,
				firstName: ctx.dbUser.firstName,
				planOrPack: `${plan.label} Subscription`,
				stars: plan.stars,
				credits: plan.credits,
				chargeId,
				isRenewal,
			}),
		);

		derpMetrics.creditRevenue.add(plan.stars, { source: "subscription" });
		derpMetrics.creditTransactions.add(1, { type: "subscription" });
	} else if (payload.type === "pack") {
		const pack = getTopUpPack(payload.packId);
		if (!pack) return;

		if (payload.target === "chat") {
			const result = await addChatCreditsWithResult(
				ctx.db,
				ctx.dbChat.id,
				ctx.dbUser.id,
				pack.credits,
				"purchase",
				chargeId,
				`pack:${chargeId}`,
				{ packId: pack.id, stars: pack.stars },
			);
			if (!result.applied) return;
			await replyWithPaymentEffect(
				ctx,
				`${pack.credits} credits added to this chat's pool!`,
			);
			await notifyAdmins(
				formatPaymentNotification({
					type: "purchase",
					userId: ctx.dbUser.telegramId,
					username: ctx.dbUser.username,
					firstName: ctx.dbUser.firstName,
					planOrPack: `${pack.label} Group Pack`,
					stars: pack.stars,
					credits: pack.credits,
					chargeId,
					chatId: ctx.dbChat.telegramId,
				}),
			);
		} else {
			const result = await addUserCreditsWithResult(
				ctx.db,
				ctx.dbUser.id,
				pack.credits,
				"purchase",
				chargeId,
				`pack:${chargeId}`,
				{ packId: pack.id, stars: pack.stars },
			);
			if (!result.applied) return;
			await replyWithPaymentEffect(
				ctx,
				`${pack.credits} credits added to your balance!`,
			);
			await notifyAdmins(
				formatPaymentNotification({
					type: "purchase",
					userId: ctx.dbUser.telegramId,
					username: ctx.dbUser.username,
					firstName: ctx.dbUser.firstName,
					planOrPack: `${pack.label} Pack`,
					stars: pack.stars,
					credits: pack.credits,
					chargeId,
				}),
			);
		}
	}
});

// ── Refunded payment ───────────────────────────────────────────────────────

creditsComposer.on("message:refunded_payment", async (ctx) => {
	const refund = ctx.message?.refunded_payment;
	if (!refund) return;

	try {
		const reconciliation = await reconcileStarRefund(
			ctx.db,
			refund.telegram_payment_charge_id,
			{
				source: "telegram_refunded_payment",
				invoicePayload: refund.invoice_payload,
				currency: refund.currency,
				totalAmount: refund.total_amount,
			},
		);
		if (!reconciliation.applied) return;

		await notifyAdmins(
			`↩️ <b>Refund reconciled</b>\n\nCharge: <code>${escapeHtml(refund.telegram_payment_charge_id)}</code>\nTarget: ${reconciliation.target}\nRecovered: ${reconciliation.recoveredAmount}/${reconciliation.originalAmount}\nUnrecovered: ${reconciliation.unrecoveredAmount}`,
		);
	} catch (err) {
		const reason = err instanceof Error ? err.message : String(err);
		await notifyAdmins(
			`⚠️ <b>Refund reconciliation failed</b>\n\nCharge: <code>${escapeHtml(refund.telegram_payment_charge_id)}</code>\nReason: ${escapeHtml(reason)}`,
		);
	}
});

export { creditsComposer };
