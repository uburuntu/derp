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
import {
	buildBuyKeyboard,
	buildBuyTargetKeyboard,
	buildGroupPackKeyboard,
	buildPersonalPackKeyboard,
	buildSubscriptionKeyboard,
	formatBalanceMessage,
} from "../credits/ui";
import { getChatByTelegramId } from "../db/queries/chats";
import {
	applyChatPackPayment,
	applySubscriptionPayment,
	applyUserPackPayment,
	getBalances,
	markPaymentSettlementFailed,
	reconcileStarRefund,
} from "../db/queries/credits";
import { getOpenDebtAmount } from "../db/queries/finance";

const creditsComposer = new Composer<DerpContext>();

function expectedStars(payload: SignedCreditPaymentPayload): number | null {
	if (payload.type === "sub") {
		return getSubscriptionPlan(payload.planId)?.stars ?? null;
	}
	return getTopUpPack(payload.packId)?.stars ?? null;
}

type PaymentValidationErrorKey =
	| "payment-error-unknown-payload"
	| "payment-error-currency"
	| "payment-error-product"
	| "payment-error-amount";

function validateStarsPayment(
	payloadText: string,
	currency: string,
	totalAmount: number,
):
	| { payload: SignedCreditPaymentPayload; stars: number }
	| { errorKey: PaymentValidationErrorKey } {
	const payload = parseCreditPaymentPayload(payloadText);
	if (!payload) return { errorKey: "payment-error-unknown-payload" };
	if (currency !== "XTR") return { errorKey: "payment-error-currency" };

	const stars = expectedStars(payload);
	if (stars == null) return { errorKey: "payment-error-product" };
	if (totalAmount !== stars) return { errorKey: "payment-error-amount" };

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
			ctx.t("payment-settlement-failed", {
				chargeId: escapeHtml(payment.telegram_payment_charge_id),
			}),
			{
				parse_mode: "HTML",
				...commandReplyOptions(ctx),
			},
		);
		await markPaymentSettlementFailed(
			ctx.db,
			payment.telegram_payment_charge_id,
			reason,
		).catch((markErr) => {
			logger.error("payment_mark_settlement_failed_failed", {
				chargeId: payment.telegram_payment_charge_id,
				error: markErr instanceof Error ? markErr.message : String(markErr),
			});
		});
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

function commandReplyOptions(ctx: DerpContext) {
	return {
		message_thread_id: messageThreadId(ctx),
		reply_to_message_id: ctx.message?.message_id,
	};
}

function isGroupChat(ctx: DerpContext): boolean {
	return ctx.chat?.type === "group" || ctx.chat?.type === "supergroup";
}

function privatePaymentChatId(ctx: DerpContext): number | null {
	if (!isGroupChat(ctx)) return ctx.chat?.id ?? null;
	return ctx.from?.id ?? null;
}

// ── /credits, /balance, /bal ────────────────────────────────────────────────

creditsComposer.command(["credits", "balance", "bal"], async (ctx) => {
	if (!ctx.dbUser || !ctx.dbChat) return;

	const { userCredits, chatCredits } = await getBalances(
		ctx.db,
		ctx.dbUser.telegramId,
		ctx.dbChat.telegramId,
	);
	const [userDebt, chatDebt] = await Promise.all([
		getOpenDebtAmount(ctx.db, { userId: ctx.dbUser.id }),
		getOpenDebtAmount(ctx.db, {
			userId: ctx.dbUser.id,
			chatId: ctx.dbChat.id,
		}),
	]);

	let message = formatBalanceMessage(
		userCredits,
		chatCredits,
		ctx.dbUser.subscriptionTier,
		ctx.dbUser.subscriptionExpiresAt,
		(key, args) => ctx.t(key, args),
	);
	if (userDebt > 0 || chatDebt > 0) {
		message += `\n\n⚠️ <b>${ctx.t("credits-refund-debt-title")}</b>`;
		if (userDebt > 0) {
			message += `\n${ctx.t("credits-personal-debt", { credits: userDebt })}`;
		}
		if (chatDebt > 0) {
			message += `\n${ctx.t("credits-group-debt", { credits: chatDebt })}`;
		}
		message += `\n<i>${ctx.t("credits-debt-hint")}</i>`;
	}
	if (ctx.chat?.type === "group" || ctx.chat?.type === "supergroup") {
		message += `\n\n<i>${ctx.t("credits-spend-order-group")}</i>`;
	}

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

	const keyboard = buildGroupPackKeyboard((key, args) => ctx.t(key, args));
	await ctx.reply(ctx.t("buy-choose-group"), {
		parse_mode: "HTML",
		reply_markup: keyboard,
		...commandReplyOptions(ctx),
	});
});

creditsComposer.callbackQuery(
	/^buy:(personal|group|subs|back|cancel)$/,
	async (ctx) => {
		const action = ctx.match[1];
		if (!action) return;
		await ctx.answerCallbackQuery();

		if (action === "cancel") {
			await ctx.deleteMessage().catch(() => undefined);
			return;
		}

		const isGroup =
			ctx.chat?.type === "group" || ctx.chat?.type === "supergroup";
		if (action === "back") {
			await ctx.editMessageText(ctx.t("buy-choose"), {
				parse_mode: "HTML",
				reply_markup: isGroup
					? buildBuyTargetKeyboard((key, args) => ctx.t(key, args))
					: buildBuyKeyboard(false, (key, args) => ctx.t(key, args)),
			});
			return;
		}

		if (action === "personal") {
			if (isGroup) {
				await ctx.editMessageText(ctx.t("buy-private-required"), {
					parse_mode: "HTML",
					reply_markup: buildBuyTargetKeyboard((key, args) => ctx.t(key, args)),
				});
				return;
			}
			await ctx.editMessageText(ctx.t("buy-choose-personal"), {
				parse_mode: "HTML",
				reply_markup: buildPersonalPackKeyboard((key, args) =>
					ctx.t(key, args),
				),
			});
			return;
		}

		if (action === "group") {
			if (!isGroup) {
				await ctx.editMessageText(ctx.t("buy-chat-groups-only"), {
					parse_mode: "HTML",
				});
				return;
			}
			await ctx.editMessageText(ctx.t("buy-choose-group"), {
				parse_mode: "HTML",
				reply_markup: buildGroupPackKeyboard((key, args) => ctx.t(key, args)),
			});
			return;
		}

		if (isGroup) {
			await ctx.editMessageText(ctx.t("buy-private-required"), {
				parse_mode: "HTML",
				reply_markup: buildBuyTargetKeyboard((key, args) => ctx.t(key, args)),
			});
			return;
		}
		await ctx.editMessageText(ctx.t("buy-choose-subscriptions"), {
			parse_mode: "HTML",
			reply_markup: buildSubscriptionKeyboard((key, args) => ctx.t(key, args)),
		});
	},
);

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
	const paymentChatId = privatePaymentChatId(ctx);
	if (paymentChatId == null) {
		await ctx.answerCallbackQuery(ctx.t("error-generic"));
		return;
	}

	// Create subscription invoice link
	let link: string;
	try {
		link = await ctx.api.createInvoiceLink(
			ctx.t("buy-invoice-sub-title", { plan: plan.label }),
			ctx.t("buy-invoice-sub-description", {
				credits: plan.credits,
				savings: plan.savings,
			}),
			buildCreditPaymentPayload({
				type: "sub",
				planId: plan.id,
				targetChatId: ctx.chat?.id ?? 0,
				targetThreadId: messageThreadId(ctx) ?? null,
			}),
			"", // empty provider_token for Stars
			"XTR",
			[
				{
					label: ctx.t("buy-invoice-sub-label", { plan: plan.label }),
					amount: plan.stars,
				},
			],
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

	try {
		await ctx.api.sendMessage(
			paymentChatId,
			ctx.t("buy-subscribe", { plan: escapeHtml(plan.label) }),
			{
				parse_mode: "HTML",
				message_thread_id: isGroupChat(ctx) ? undefined : messageThreadId(ctx),
				reply_markup: {
					inline_keyboard: [
						[
							{
								text: ctx.t("buy-pay-button", { stars: plan.stars }),
								url: link,
							},
						],
					],
				},
			},
		);
		if (isGroupChat(ctx)) {
			await ctx.answerCallbackQuery(ctx.t("buy-private-sent"));
		} else {
			await ctx.answerCallbackQuery();
		}
	} catch (err) {
		const reason = err instanceof Error ? err.message : String(err);
		recordHandledFailure("payment_invoice", reason, {
			reason_code: "private_subscription",
		});
		logger.error("subscription_private_message_failed", {
			planId: plan.id,
			error: reason,
		});
		await ctx.answerCallbackQuery({
			text: ctx.t("buy-private-open-bot"),
			show_alert: true,
		});
	}
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
	const paymentChatId = privatePaymentChatId(ctx);
	if (paymentChatId == null) {
		await ctx.answerCallbackQuery(ctx.t("error-generic"));
		return;
	}

	try {
		await ctx.api.sendInvoice(
			paymentChatId,
			ctx.t("buy-invoice-pack-title", { pack: pack.label }),
			ctx.t("buy-invoice-pack-description", { credits: pack.credits }),
			buildCreditPaymentPayload({
				type: "pack",
				packId: pack.id,
				target: "user",
				targetChatId: paymentChatId,
				targetThreadId: isGroupChat(ctx)
					? null
					: (messageThreadId(ctx) ?? null),
			}),
			"XTR",
			[
				{
					label: ctx.t("buy-invoice-pack-label", { pack: pack.label }),
					amount: pack.stars,
				},
			],
			{
				provider_token: "",
				message_thread_id: isGroupChat(ctx) ? undefined : messageThreadId(ctx),
			},
		);
		if (isGroupChat(ctx)) {
			await ctx.answerCallbackQuery(ctx.t("buy-private-sent"));
		} else {
			await ctx.answerCallbackQuery();
		}
	} catch (err) {
		const reason = err instanceof Error ? err.message : String(err);
		recordHandledFailure("payment_invoice", reason, {
			reason_code: "pack_invoice",
		});
		logger.error("pack_invoice_failed", { packId: pack.id, error: reason });
		if (isGroupChat(ctx)) {
			await ctx.answerCallbackQuery({
				text: ctx.t("buy-private-open-bot"),
				show_alert: true,
			});
		} else {
			await ctx.answerCallbackQuery();
			await ctx.reply(ctx.t("buy-invoice-error"), {
				parse_mode: "HTML",
				message_thread_id: messageThreadId(ctx),
			});
		}
	}
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
	try {
		await ctx.api.sendInvoice(
			ctx.chat.id,
			ctx.t("buy-invoice-group-pack-title", { pack: pack.label }),
			ctx.t("buy-invoice-group-pack-description", {
				credits: pack.credits,
			}),
			buildCreditPaymentPayload({
				type: "pack",
				packId: pack.id,
				target: "chat",
				targetChatId: ctx.chat.id,
				targetThreadId: messageThreadId(ctx) ?? null,
			}),
			"XTR",
			[
				{
					label: ctx.t("buy-invoice-group-pack-label", { pack: pack.label }),
					amount: pack.stars,
				},
			],
			{ provider_token: "", message_thread_id: messageThreadId(ctx) },
		);
	} catch (err) {
		const reason = err instanceof Error ? err.message : String(err);
		recordHandledFailure("payment_invoice", reason, {
			reason_code: "group_pack_invoice",
		});
		logger.error("group_pack_invoice_failed", {
			packId: pack.id,
			error: reason,
		});
		await ctx.reply(ctx.t("buy-invoice-error"), {
			parse_mode: "HTML",
			message_thread_id: messageThreadId(ctx),
		});
	}
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
	if ("errorKey" in validation) {
		await ctx.answerPreCheckoutQuery(
			false,
			ctx.t("payment-validation-error", { reason: ctx.t(validation.errorKey) }),
		);
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
	if ("errorKey" in validation) {
		await ctx.reply(
			ctx.t("payment-validation-error", {
				reason: escapeHtml(ctx.t(validation.errorKey)),
			}),
			{
				parse_mode: "HTML",
				...commandReplyOptions(ctx),
			},
		);
		await notifyAdmins(
			`⚠️ <b>Payment validation failed</b>\n\nUser: <code>${ctx.dbUser.telegramId}</code>\nPayload: <code>${escapeHtml(payment.invoice_payload)}</code>\nReason: ${escapeHtml(validation.errorKey)}`,
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

		const msg =
			result.debtRecovered && result.debtRecovered > 0
				? ctx.t(
						isRenewal ? "payment-sub-renewed-debt" : "payment-sub-new-debt",
						{
							plan: plan.label,
							debt: result.debtRecovered,
							credits: result.creditedAmount ?? 0,
						},
					)
				: ctx.t(isRenewal ? "payment-sub-renewed" : "payment-sub-new", {
						plan: plan.label,
						credits: plan.credits,
					});
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
			targetChatId: ctx.chat?.id ?? payload.targetChatId,
			targetThreadId: messageThreadId(ctx) ?? null,
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
					chatId: targetChat?.id ?? null,
					telegramChargeId: chargeId,
					providerChargeId: payment.provider_payment_charge_id,
					invoicePayload: payment.invoice_payload,
					currency: payment.currency,
					stars: pack.stars,
					productType: "pack",
					productId: pack.id,
					creditTarget: "chat",
					credits: pack.credits,
					meta: { packId: pack.id, targetTelegramChatId: payload.targetChatId },
				}),
			);
			if (!result?.applied) return;
			await runAppliedPaymentSideEffects(ctx, payment, {
				replyText:
					result.debtRecovered && result.debtRecovered > 0
						? ctx.t("payment-pack-chat-debt", {
								debt: result.debtRecovered,
								credits: result.creditedAmount ?? 0,
							})
						: ctx.t("payment-pack-chat", { credits: pack.credits }),
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
				replyText:
					result.debtRecovered && result.debtRecovered > 0
						? ctx.t("payment-pack-user-debt", {
								debt: result.debtRecovered,
								credits: result.creditedAmount ?? 0,
							})
						: ctx.t("payment-pack-user", { credits: pack.credits }),
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
				targetChatId: ctx.chat?.id ?? payload.targetChatId,
				targetThreadId: messageThreadId(ctx) ?? null,
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
		if (reconciliation.applied) {
			await notifyAdmins(
				`↩️ <b>Refund reconciled</b>\n\nCharge: <code>${escapeHtml(refund.telegram_payment_charge_id)}</code>\nTarget: ${reconciliation.target}\nRecovered: ${reconciliation.recoveredAmount}/${reconciliation.originalAmount}\nUnrecovered: ${reconciliation.unrecoveredAmount}`,
				{ critical: true },
			);
		}
		const target = ctx.t(
			reconciliation.target === "chat"
				? "refund-target-chat"
				: "refund-target-user",
		);
		await ctx.reply(
			ctx.t(
				reconciliation.unrecoveredAmount > 0
					? "refund-processed-debt"
					: "refund-processed",
				{
					recovered: reconciliation.recoveredAmount,
					original: reconciliation.originalAmount,
					target,
					debt: reconciliation.unrecoveredAmount,
				},
			),
			{
				parse_mode: "HTML",
				...commandReplyOptions(ctx),
			},
		);
	} catch (err) {
		const reason = err instanceof Error ? err.message : String(err);
		recordHandledFailure("refund", reason, {
			chargeId: refund.telegram_payment_charge_id,
		});
		await ctx.reply(
			ctx.t("refund-review-needed", {
				chargeId: escapeHtml(refund.telegram_payment_charge_id),
			}),
			{
				parse_mode: "HTML",
				...commandReplyOptions(ctx),
			},
		);
		await notifyAdmins(
			`⚠️ <b>Refund reconciliation failed</b>\n\nCharge: <code>${escapeHtml(refund.telegram_payment_charge_id)}</code>\nReason: ${escapeHtml(reason)}`,
			{ critical: true },
		);
	}
});

export { creditsComposer };
