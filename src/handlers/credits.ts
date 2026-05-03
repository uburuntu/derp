/** Credits handler — /credits, /buy, payment flows */

import { Composer } from "grammy";
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
import { getTopUpPack } from "../credits/packs";
import {
	buildCreditPaymentPayload,
	parseCreditPaymentPayload,
	type SignedCreditPaymentPayload,
} from "../credits/payment-payload";
import { getSubscriptionPlan } from "../credits/subscriptions";
import { buildBuyKeyboard, formatBalanceMessage } from "../credits/ui";
import { getChatByTelegramId } from "../db/queries/chats";
import {
	applyChatPackPayment,
	applySubscriptionPayment,
	applyUserPackPayment,
	getBalances,
	reconcileStarRefund,
} from "../db/queries/credits";

const creditsComposer = new Composer<DerpContext>();

function expectedStars(payload: SignedCreditPaymentPayload): number | null {
	if (payload.type === "sub") {
		return getSubscriptionPlan(payload.planId)?.stars ?? null;
	}
	return getTopUpPack(payload.packId)?.stars ?? null;
}

function validateStarsPayment(
	payloadText: string,
	currency: string,
	totalAmount: number,
): { payload: SignedCreditPaymentPayload; stars: number } | { error: string } {
	const payload = parseCreditPaymentPayload(payloadText);
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
	target?: { chatId: number; threadId: number | null },
): Promise<void> {
	const options = {
		parse_mode: "HTML" as const,
		message_thread_id: target?.threadId ?? ctx.message?.message_thread_id,
	};
	const chatId = target?.chatId ?? ctx.chat?.id;
	if (chatId == null) throw new Error("No chat for payment acknowledgement");
	try {
		await ctx.api.sendMessage(chatId, text, {
			...options,
			message_effect_id: MESSAGE_EFFECTS.party,
		});
	} catch {
		await ctx.api.sendMessage(chatId, text, options);
	}
}

type SuccessfulPaymentLike = {
	telegram_payment_charge_id: string;
	invoice_payload: string;
	total_amount: number;
	currency: string;
};

type PaymentSideEffects = {
	replyText: string;
	adminText: string;
	revenueStars: number;
	revenueSource: string;
	transactionType: string;
	targetChatId: number;
	targetThreadId: number | null;
};

async function applyPaymentOrReport<T extends { applied: boolean }>(
	ctx: DerpContext,
	payment: SuccessfulPaymentLike,
	applyPayment: () => Promise<T>,
): Promise<T | null> {
	try {
		return await applyPayment();
	} catch (err) {
		const reason = err instanceof Error ? err.message : String(err);
		recordHandledFailure("payment", reason, {
			reason_code: "db_apply",
		});
		logger.error("payment_apply_failed", {
			userId: ctx.dbUser?.telegramId,
			chatId: ctx.dbChat?.telegramId,
			chargeId: payment.telegram_payment_charge_id,
			payload: payment.invoice_payload,
			error: reason,
		});
		await ctx.reply(
			`⚠️ <b>Payment received</b>\n\nI could not update the credit balance automatically. Charge: <code>${escapeHtml(payment.telegram_payment_charge_id)}</code>.`,
			{
				parse_mode: "HTML",
				...commandReplyOptions(ctx),
			},
		);
		await notifyAdmins(
			`⚠️ <b>Payment processing failed</b>\n\nUser: <code>${ctx.dbUser?.telegramId ?? "unknown"}</code>\nChat: <code>${ctx.dbChat?.telegramId ?? "unknown"}</code>\nCharge: <code>${escapeHtml(payment.telegram_payment_charge_id)}</code>\nPayload: <code>${escapeHtml(payment.invoice_payload)}</code>\nAmount: ${payment.total_amount} ${escapeHtml(payment.currency)}\nReason: ${escapeHtml(reason)}`,
			{ critical: true },
		);
		return null;
	}
}

async function runAppliedPaymentSideEffects(
	ctx: DerpContext,
	payment: SuccessfulPaymentLike,
	effects: PaymentSideEffects,
): Promise<void> {
	const failures: string[] = [];

	try {
		await replyWithPaymentEffect(ctx, effects.replyText, {
			chatId: effects.targetChatId,
			threadId: effects.targetThreadId,
		});
	} catch (err) {
		failures.push(`reply: ${err instanceof Error ? err.message : String(err)}`);
	}

	try {
		await notifyAdmins(effects.adminText);
	} catch (err) {
		failures.push(`admin: ${err instanceof Error ? err.message : String(err)}`);
	}

	try {
		derpMetrics.creditRevenue.add(effects.revenueStars, {
			source: effects.revenueSource,
		});
		derpMetrics.creditTransactions.add(1, { type: effects.transactionType });
	} catch (err) {
		failures.push(
			`metrics: ${err instanceof Error ? err.message : String(err)}`,
		);
	}

	if (failures.length === 0) return;

	const reason = failures.join("; ");
	recordHandledFailure("payment_notification", reason, {
		reason_code: "post_commit",
	});
	logger.error("payment_post_commit_side_effect_failed", {
		userId: ctx.dbUser?.telegramId,
		chatId: ctx.dbChat?.telegramId,
		chargeId: payment.telegram_payment_charge_id,
		failures,
	});
	await notifyAdmins(
		`⚠️ <b>Payment applied, notification failed</b>\n\nUser: <code>${ctx.dbUser?.telegramId ?? "unknown"}</code>\nCharge: <code>${escapeHtml(payment.telegram_payment_charge_id)}</code>\nPayload: <code>${escapeHtml(payment.invoice_payload)}</code>\nIssue: ${escapeHtml(reason)}`,
		{ critical: true },
	).catch((err) => {
		logger.error("payment_post_commit_admin_notify_failed", {
			chargeId: payment.telegram_payment_charge_id,
			error: err instanceof Error ? err.message : String(err),
		});
	});
}

type MaybeThreadedMessage = {
	message_id?: number;
	message_thread_id?: number;
};

function callbackMessage(ctx: DerpContext): MaybeThreadedMessage | undefined {
	const message = ctx.callbackQuery?.message;
	if (!message || !("message_id" in message)) return undefined;
	return message as MaybeThreadedMessage;
}

function messageThreadId(ctx: DerpContext): number | undefined {
	return (
		ctx.message?.message_thread_id ?? callbackMessage(ctx)?.message_thread_id
	);
}

function requireTargetChatId(
	chat: Awaited<ReturnType<typeof getChatByTelegramId>>,
): string {
	if (!chat) throw new Error("Payment target chat not found");
	return chat.id;
}

function commandReplyOptions(ctx: DerpContext) {
	return {
		message_thread_id: messageThreadId(ctx),
		reply_to_message_id: ctx.message?.message_id,
	};
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
		...commandReplyOptions(ctx),
	});
});

// ── /buy ────────────────────────────────────────────────────────────────────

creditsComposer.command(["buy", "purchase", "shop"], async (ctx) => {
	const isGroup = ctx.chat?.type === "group" || ctx.chat?.type === "supergroup";
	const keyboard = buildBuyKeyboard(isGroup, (key, args) => ctx.t(key, args));

	await ctx.reply(ctx.t("buy-choose"), {
		parse_mode: "HTML",
		reply_markup: keyboard,
		...commandReplyOptions(ctx),
	});
});

creditsComposer.command(["buy_chat", "buychat"], async (ctx) => {
	const isGroup = ctx.chat?.type === "group" || ctx.chat?.type === "supergroup";
	if (!isGroup) {
		await ctx.reply(ctx.t("buy-chat-groups-only"), {
			parse_mode: "HTML",
			...commandReplyOptions(ctx),
		});
		return;
	}

	const keyboard = buildBuyKeyboard(true, (key, args) => ctx.t(key, args));
	await ctx.reply(ctx.t("buy-choose"), {
		parse_mode: "HTML",
		reply_markup: keyboard,
		...commandReplyOptions(ctx),
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
	if (!ctx.chat) {
		await ctx.answerCallbackQuery(ctx.t("error-generic"));
		return;
	}
	await ctx.answerCallbackQuery();

	// Create subscription invoice link
	let link: string;
	try {
		link = await ctx.api.createInvoiceLink(
			`${plan.label} Subscription`,
			`${plan.credits} credits/month (${plan.savings} savings)`,
			buildCreditPaymentPayload({
				type: "sub",
				planId: plan.id,
				targetChatId: ctx.chat?.id ?? 0,
				targetThreadId: messageThreadId(ctx) ?? null,
			}),
			"", // empty provider_token for Stars
			"XTR",
			[{ label: `${plan.label} Subscription`, amount: plan.stars }],
			{ subscription_period: 2592000 },
		);
	} catch (err) {
		const reason = err instanceof Error ? err.message : String(err);
		recordHandledFailure("payment_invoice", reason, {
			reason_code: "invoice_link",
		});
		logger.error("subscription_invoice_link_failed", {
			planId: plan.id,
			error: reason,
		});
		await ctx.reply(ctx.t("buy-invoice-error"), {
			parse_mode: "HTML",
			message_thread_id: messageThreadId(ctx),
		});
		return;
	}

	await ctx.reply(ctx.t("buy-subscribe", { plan: escapeHtml(plan.label) }), {
		parse_mode: "HTML",
		message_thread_id: messageThreadId(ctx),
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
	if (!ctx.chat) {
		await ctx.answerCallbackQuery(ctx.t("error-generic"));
		return;
	}

	await ctx.answerCallbackQuery();
	await ctx.api.sendInvoice(
		ctx.chat.id,
		`${pack.label} Credit Pack`,
		`${pack.credits} credits`,
		buildCreditPaymentPayload({
			type: "pack",
			packId: pack.id,
			target: "user",
			targetChatId: ctx.chat.id,
			targetThreadId: messageThreadId(ctx) ?? null,
		}),
		"XTR",
		[{ label: `${pack.label} Pack`, amount: pack.stars }],
		{ provider_token: "", message_thread_id: messageThreadId(ctx) },
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
	if (!ctx.chat) {
		await ctx.answerCallbackQuery(ctx.t("error-generic"));
		return;
	}

	await ctx.answerCallbackQuery();
	await ctx.api.sendInvoice(
		ctx.chat.id,
		`${pack.label} Group Credit Pack`,
		`${pack.credits} credits for this chat`,
		buildCreditPaymentPayload({
			type: "pack",
			packId: pack.id,
			target: "chat",
			targetChatId: ctx.chat.id,
			targetThreadId: messageThreadId(ctx) ?? null,
		}),
		"XTR",
		[{ label: `${pack.label} Group Pack`, amount: pack.stars }],
		{ provider_token: "", message_thread_id: messageThreadId(ctx) },
	);
});

// ── Pre-checkout query ──────────────────────────────────────────────────────

creditsComposer.on("pre_checkout_query", async (ctx, next) => {
	const query = ctx.preCheckoutQuery;
	if (query.invoice_payload.startsWith("donate:")) return next();

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

creditsComposer.on("message:successful_payment", async (ctx, next) => {
	if (!ctx.dbUser || !ctx.dbChat) return;

	const payment = ctx.message?.successful_payment;
	if (!payment) return;
	if (payment.invoice_payload.startsWith("donate:")) return next();

	const validation = validateStarsPayment(
		payment.invoice_payload,
		payment.currency,
		payment.total_amount,
	);
	if ("error" in validation) {
		await ctx.reply(`Payment rejected: ${escapeHtml(validation.error)}`, {
			parse_mode: "HTML",
			...commandReplyOptions(ctx),
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

		const result = await applyPaymentOrReport(ctx, payment, () =>
			applySubscriptionPayment(
				ctx.db,
				ctx.dbUser.id,
				plan.credits,
				plan.id,
				chargeId,
				newExpiry,
				{
					chatId: null,
					providerChargeId: payment.provider_payment_charge_id,
					invoicePayload: payment.invoice_payload,
					currency: payment.currency,
					stars: plan.stars,
				},
				{
					planId: plan.id,
					stars: plan.stars,
					isRenewal,
					isRecurring: subscriptionFields.is_recurring === true,
					isFirstRecurring: subscriptionFields.is_first_recurring === true,
					telegramSubscriptionExpirationDate:
						subscriptionFields.subscription_expiration_date,
				},
			),
		);
		if (!result?.applied) return;

		const msg = isRenewal
			? `${plan.label} subscription renewed! ${plan.credits} credits added.`
			: `Subscribed to ${plan.label}! ${plan.credits} credits added. Your subscription renews monthly.`;
		await runAppliedPaymentSideEffects(ctx, payment, {
			replyText: msg,
			adminText: formatPaymentNotification({
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
			revenueStars: plan.stars,
			revenueSource: "subscription",
			transactionType: "subscription",
			targetChatId: payload.targetChatId,
			targetThreadId: payload.targetThreadId,
		});
	} else if (payload.type === "pack") {
		const pack = getTopUpPack(payload.packId);
		if (!pack) return;

		if (payload.target === "chat") {
			const targetChat = await getChatByTelegramId(
				ctx.db,
				payload.targetChatId,
			);
			const result = await applyPaymentOrReport(ctx, payment, () =>
				applyChatPackPayment(ctx.db, {
					userId: ctx.dbUser.id,
					chatId: requireTargetChatId(targetChat),
					telegramChargeId: chargeId,
					providerChargeId: payment.provider_payment_charge_id,
					invoicePayload: payment.invoice_payload,
					currency: payment.currency,
					stars: pack.stars,
					productType: "pack",
					productId: pack.id,
					creditTarget: "chat",
					credits: pack.credits,
					meta: { packId: pack.id },
				}),
			);
			if (!result?.applied) return;
			await runAppliedPaymentSideEffects(ctx, payment, {
				replyText: `${pack.credits} credits added to this chat's pool!`,
				adminText: formatPaymentNotification({
					type: "purchase",
					userId: ctx.dbUser.telegramId,
					username: ctx.dbUser.username,
					firstName: ctx.dbUser.firstName,
					planOrPack: `${pack.label} Group Pack`,
					stars: pack.stars,
					credits: pack.credits,
					chargeId,
					chatId: payload.targetChatId,
				}),
				revenueStars: pack.stars,
				revenueSource: "pack_chat",
				transactionType: "purchase",
				targetChatId: payload.targetChatId,
				targetThreadId: payload.targetThreadId,
			});
		} else {
			const result = await applyPaymentOrReport(ctx, payment, () =>
				applyUserPackPayment(ctx.db, {
					userId: ctx.dbUser.id,
					chatId: null,
					telegramChargeId: chargeId,
					providerChargeId: payment.provider_payment_charge_id,
					invoicePayload: payment.invoice_payload,
					currency: payment.currency,
					stars: pack.stars,
					productType: "pack",
					productId: pack.id,
					creditTarget: "user",
					credits: pack.credits,
					meta: { packId: pack.id },
				}),
			);
			if (!result?.applied) return;
			await runAppliedPaymentSideEffects(ctx, payment, {
				replyText: `${pack.credits} credits added to your balance!`,
				adminText: formatPaymentNotification({
					type: "purchase",
					userId: ctx.dbUser.telegramId,
					username: ctx.dbUser.username,
					firstName: ctx.dbUser.firstName,
					planOrPack: `${pack.label} Pack`,
					stars: pack.stars,
					credits: pack.credits,
					chargeId,
				}),
				revenueStars: pack.stars,
				revenueSource: "pack_user",
				transactionType: "purchase",
				targetChatId: payload.targetChatId,
				targetThreadId: payload.targetThreadId,
			});
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
			{ critical: true },
		);
	} catch (err) {
		const reason = err instanceof Error ? err.message : String(err);
		recordHandledFailure("refund", reason, {
			chargeId: refund.telegram_payment_charge_id,
		});
		await notifyAdmins(
			`⚠️ <b>Refund reconciliation failed</b>\n\nCharge: <code>${escapeHtml(refund.telegram_payment_charge_id)}</code>\nReason: ${escapeHtml(reason)}`,
			{ critical: true },
		);
	}
});

export { creditsComposer };
