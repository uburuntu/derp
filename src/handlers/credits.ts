/** Credits handler — /credits, /buy, payment flows */

import { Composer } from "grammy";
import type { DerpContext } from "../bot/context";
import {
	formatPaymentNotification,
	notifyAdmins,
} from "../common/admin-notify";
import { MESSAGE_EFFECTS } from "../common/telegram";
import { derpMetrics } from "../common/observability";
import { getTopUpPack } from "../credits/packs";
import { getSubscriptionPlan } from "../credits/subscriptions";
import { buildBuyKeyboard, formatBalanceMessage } from "../credits/ui";
import {
	addChatCredits,
	addUserCredits,
	deductUserCredits,
	getBalances,
} from "../db/queries/credits";

const creditsComposer = new Composer<DerpContext>();

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
	);

	await ctx.reply(message, {
		parse_mode: "HTML",
		reply_to_message_id: ctx.message?.message_id,
	});
});

// ── /buy ────────────────────────────────────────────────────────────────────

creditsComposer.command("buy", async (ctx) => {
	const isGroup = ctx.chat?.type === "group" || ctx.chat?.type === "supergroup";
	const keyboard = buildBuyKeyboard(isGroup);

	await ctx.reply(ctx.t("buy-choose"), {
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
		await ctx.answerCallbackQuery("Plan not found");
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
	await ctx.reply(`Subscribe to ${plan.label}:`, {
		reply_markup: {
			inline_keyboard: [[{ text: `Pay ${plan.stars}⭐/month`, url: link }]],
		},
	});
});

// ── Callback: top-up pack selection (personal) ──────────────────────────────

creditsComposer.callbackQuery(/^pack:(.+)$/, async (ctx) => {
	const packId = ctx.match[1];
	if (!packId) return;
	const pack = getTopUpPack(packId);
	if (!pack) {
		await ctx.answerCallbackQuery("Pack not found");
		return;
	}

	await ctx.answerCallbackQuery();
	await ctx.api.sendInvoice(
		ctx.chat!.id,
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
		await ctx.answerCallbackQuery("Pack not found");
		return;
	}

	await ctx.answerCallbackQuery();
	await ctx.api.sendInvoice(
		ctx.chat!.id,
		`${pack.label} Group Credit Pack`,
		`${pack.credits} credits for this chat`,
		`pack:${pack.id}:chat`,
		"XTR",
		[{ label: `${pack.label} Group Pack`, amount: pack.stars }],
		{ provider_token: "" },
	);
});

// ── Callback: noop (separator buttons) ──────────────────────────────────────

creditsComposer.callbackQuery("noop", async (ctx) => {
	await ctx.answerCallbackQuery();
});

// ── Callback: credit transfer (personal → group pool) ──────────────────────

creditsComposer.callbackQuery("transfer", async (ctx) => {
	if (!ctx.dbUser || !ctx.dbChat) return;

	const isGroup = ctx.chat?.type === "group" || ctx.chat?.type === "supergroup";
	if (!isGroup) {
		await ctx.answerCallbackQuery("Transfers only work in groups");
		return;
	}

	const { userCredits } = await getBalances(
		ctx.db,
		ctx.dbUser.telegramId,
		ctx.dbChat.telegramId,
	);

	if (userCredits < 100) {
		await ctx.answerCallbackQuery("Minimum transfer: 100 credits");
		return;
	}

	await ctx.answerCallbackQuery();
	await ctx.reply(
		`Transfer credits to this chat's pool.\nYour balance: ${userCredits}\nMinimum: 100\n\nReply with the amount to transfer:`,
	);
});

// Handle transfer amount as a reply
creditsComposer.hears(/^\d+$/, async (ctx) => {
	// Only handle if replying to a transfer prompt from the bot
	if (!ctx.dbUser || !ctx.dbChat) return;
	if (!ctx.message?.reply_to_message?.from?.is_bot) return;
	if (!ctx.message.reply_to_message.text?.includes("Transfer credits")) return;

	const amount = Number.parseInt(ctx.message.text!, 10);
	if (Number.isNaN(amount) || amount < 100) {
		await ctx.reply("Minimum transfer: 100 credits");
		return;
	}

	const { userCredits } = await getBalances(
		ctx.db,
		ctx.dbUser.telegramId,
		ctx.dbChat.telegramId,
	);

	if (userCredits < amount) {
		await ctx.reply(`Insufficient credits. You have ${userCredits}.`);
		return;
	}

	try {
		await deductUserCredits(
			ctx.db,
			ctx.dbUser.id,
			amount,
			"transfer",
			null,
			`transfer:${ctx.dbUser.id}:${ctx.dbChat.id}:${Date.now()}`,
		);
		await addChatCredits(
			ctx.db,
			ctx.dbChat.id,
			ctx.dbUser.id,
			amount,
			"transfer",
			undefined,
			`transfer_in:${ctx.dbUser.id}:${ctx.dbChat.id}:${Date.now()}`,
		);
		await ctx.reply(`Transferred ${amount} credits to this chat's pool.`);
	} catch (err) {
		const msg = err instanceof Error ? err.message : String(err);
		await ctx.reply(`Transfer failed: ${msg}`);
	}
});

// ── Pre-checkout query ──────────────────────────────────────────────────────

creditsComposer.on("pre_checkout_query", async (ctx) => {
	// Always accept — validation happens on successful_payment
	await ctx.answerPreCheckoutQuery(true);
});

// ── Successful payment ──────────────────────────────────────────────────────

creditsComposer.on("message:successful_payment", async (ctx) => {
	if (!ctx.dbUser || !ctx.dbChat) return;

	const payment = ctx.message!.successful_payment!;
	const payload = payment.invoice_payload;
	const chargeId = payment.telegram_payment_charge_id;

	// Parse payload: "sub:pro" or "pack:medium:user" or "pack:medium:chat"
	const parts = payload.split(":");
	const type = parts[0];

	if (type === "sub") {
		// Subscription payment
		const planId = parts[1];
		if (!planId) return;
		const plan = getSubscriptionPlan(planId);
		if (!plan) return;

		// Grant credits
		await addUserCredits(
			ctx.db,
			ctx.dbUser.id,
			plan.credits,
			"subscription",
			chargeId,
			`sub:${chargeId}`,
			{ planId: plan.id, stars: plan.stars },
		);

		// Update subscription status on user
		// On renewal: extend from current expiry, not from now
		const isRenewal =
			"is_recurring" in payment &&
			(payment as { is_recurring?: boolean }).is_recurring === true;
		const currentExpiry = ctx.dbUser.subscriptionExpiresAt;
		const thirtyDays = 30 * 24 * 60 * 60 * 1000;
		const newExpiry =
			isRenewal && currentExpiry && currentExpiry > new Date()
				? new Date(currentExpiry.getTime() + thirtyDays)
				: new Date(Date.now() + thirtyDays);

		const { eq } = await import("drizzle-orm");
		const { users } = await import("../db/schema");
		await ctx.db
			.update(users)
			.set({
				subscriptionTier: plan.id,
				subscriptionExpiresAt: newExpiry,
			})
			.where(eq(users.id, ctx.dbUser.id));

		const msg = isRenewal
			? `${plan.label} subscription renewed! ${plan.credits} credits added.`
			: `Subscribed to ${plan.label}! ${plan.credits} credits added. Your subscription renews monthly.`;
		await ctx.reply(msg, { message_effect_id: MESSAGE_EFFECTS.party });

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
	} else if (type === "pack") {
		const packId = parts[1];
		const target = parts[2]; // "user" or "chat"
		if (!packId) return;
		const pack = getTopUpPack(packId);
		if (!pack) return;

		if (target === "chat") {
			await addChatCredits(
				ctx.db,
				ctx.dbChat.id,
				ctx.dbUser.id,
				pack.credits,
				"purchase",
				chargeId,
				`pack:${chargeId}`,
				{ packId: pack.id, stars: pack.stars },
			);
			await ctx.reply(`${pack.credits} credits added to this chat's pool!`, {
				message_effect_id: MESSAGE_EFFECTS.party,
			});
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
			await addUserCredits(
				ctx.db,
				ctx.dbUser.id,
				pack.credits,
				"purchase",
				chargeId,
				`pack:${chargeId}`,
				{ packId: pack.id, stars: pack.stars },
			);
			await ctx.reply(`${pack.credits} credits added to your balance!`, {
				message_effect_id: MESSAGE_EFFECTS.party,
			});
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

export { creditsComposer };
