/** Credit UI utilities — inline keyboards for /buy */

import { InlineKeyboard } from "grammy";
import { TOPUP_PACKS } from "./packs";
import { SUBSCRIPTION_PLANS } from "./subscriptions";

type Translator = (
	key: string,
	args?: Record<string, string | number>,
) => string;

export function buildBuyTargetKeyboard(t: Translator): InlineKeyboard {
	return new InlineKeyboard()
		.text(t("buy-target-personal"), "buy:personal")
		.row()
		.text(t("buy-target-group"), "buy:group")
		.row()
		.text(t("buy-target-subscriptions"), "buy:subs")
		.row()
		.text(t("settings-close"), "buy:cancel");
}

export function buildSubscriptionKeyboard(t: Translator): InlineKeyboard {
	const kb = new InlineKeyboard();

	for (const plan of SUBSCRIPTION_PLANS) {
		const tag = plan.tag ? ` [${plan.tag}]` : "";
		const label = t("buy-plan-button", {
			plan: plan.label,
			stars: plan.stars,
			credits: plan.credits,
			savings: plan.savings,
			tag,
		});
		kb.text(label, `sub:${plan.id}`).row();
	}
	kb.text(t("settings-back"), "buy:back");
	return kb;
}

export function buildPersonalPackKeyboard(t: Translator): InlineKeyboard {
	const kb = new InlineKeyboard();

	for (const pack of TOPUP_PACKS) {
		const bonus = pack.bonus ? ` ${pack.bonus}` : "";
		const label = t("buy-pack-button", {
			pack: pack.label,
			stars: pack.stars,
			credits: pack.credits,
			bonus,
		});
		kb.text(label, `pack:${pack.id}`).row();
	}
	kb.text(t("settings-back"), "buy:back");
	return kb;
}

export function buildGroupPackKeyboard(t: Translator): InlineKeyboard {
	const kb = new InlineKeyboard();
	for (const pack of TOPUP_PACKS) {
		const bonus = pack.bonus ? ` ${pack.bonus}` : "";
		const label = t("buy-group-pack-button", {
			pack: pack.label,
			stars: pack.stars,
			credits: pack.credits,
			bonus,
		});
		kb.text(label, `group_pack:${pack.id}`).row();
	}
	kb.text(t("settings-back"), "buy:back");
	return kb;
}

/** Build the /buy inline keyboard with subscriptions first, then packs */
export function buildBuyKeyboard(
	isGroup: boolean,
	t: Translator,
): InlineKeyboard {
	if (isGroup) return buildBuyTargetKeyboard(t);
	const kb = buildSubscriptionKeyboard(t);
	for (const row of buildPersonalPackKeyboard(t).inline_keyboard.slice(0, -1)) {
		kb.row();
		for (const button of row) {
			kb.add(button);
		}
	}
	return kb;
}

/** Format a balance display message (Telegram HTML) */
export function formatBalanceMessage(
	userCredits: number,
	chatCredits: number,
	subscriptionTier: string | null,
	subscriptionExpiresAt: Date | null,
	t: Translator,
): string {
	const lines: string[] = [`💰 <b>${t("credits-title")}</b>\n`];

	lines.push(`<b>${t("credits-balance", { userCredits })}</b>`);

	if (chatCredits > 0) {
		lines.push(`<b>${t("credits-chat-pool", { chatCredits })}</b>`);
	}

	if (subscriptionTier && subscriptionExpiresAt) {
		const isActive = subscriptionExpiresAt > new Date();
		if (isActive) {
			const pad = (n: number) => String(n).padStart(2, "0");
			const d = subscriptionExpiresAt;
			const expiry = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
			lines.push(
				`<b>${t("credits-subscription", {
					tier: subscriptionTier.toUpperCase(),
					expiry,
				})}</b>`,
			);
		} else {
			lines.push(`<b>${t("credits-subscription-expired")}</b>`);
		}
	}

	return lines.join("\n");
}
