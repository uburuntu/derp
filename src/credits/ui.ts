/** Credit UI utilities — inline keyboards for /buy */

import { InlineKeyboard } from "grammy";
import { TOPUP_PACKS } from "./packs";
import { SUBSCRIPTION_PLANS } from "./subscriptions";

type Translator = (
	key: string,
	args?: Record<string, string | number>,
) => string;

/** Build the /buy inline keyboard with subscriptions first, then packs */
export function buildBuyKeyboard(
	isGroup: boolean,
	t: Translator,
): InlineKeyboard {
	const kb = new InlineKeyboard();

	// Subscriptions section
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

	// Top-up packs
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

	// Group-specific: fund the group pool
	if (isGroup) {
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
		kb.text(t("buy-transfer-button"), "transfer").row();
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
