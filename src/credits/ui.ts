/** Credit UI utilities — inline keyboards for /buy */

import { InlineKeyboard } from "grammy";
import { TOPUP_PACKS } from "./packs";
import { SUBSCRIPTION_PLANS } from "./subscriptions";

/** Build the /buy inline keyboard with subscriptions first, then packs */
export function buildBuyKeyboard(isGroup: boolean): InlineKeyboard {
	const kb = new InlineKeyboard();

	// Subscriptions section
	for (const plan of SUBSCRIPTION_PLANS) {
		const tag = plan.tag ? ` [${plan.tag}]` : "";
		const label = `⭐ ${plan.label} — ${plan.stars}⭐/mo → ${plan.credits} credits (${plan.savings} off)${tag}`;
		kb.text(label, `sub:${plan.id}`).row();
	}

	// Separator
	kb.text("── Credit Packs (one-time) ──", "noop").row();

	// Top-up packs
	for (const pack of TOPUP_PACKS) {
		const bonus = pack.bonus ? ` ${pack.bonus}` : "";
		const label = `${pack.label} — ${pack.stars}⭐ → ${pack.credits} credits${bonus}`;
		kb.text(label, `pack:${pack.id}`).row();
	}

	// Group-specific: fund the group pool
	if (isGroup) {
		kb.text("── Fund this group ──", "noop").row();
		for (const pack of TOPUP_PACKS) {
			const bonus = pack.bonus ? ` ${pack.bonus}` : "";
			const label = `Group: ${pack.label} — ${pack.stars}⭐ → ${pack.credits}${bonus}`;
			kb.text(label, `group_pack:${pack.id}`).row();
		}
		kb.text("Transfer from my balance (min 100)", "transfer").row();
	}

	return kb;
}

/** Format a balance display message (Telegram HTML) */
export function formatBalanceMessage(
	userCredits: number,
	chatCredits: number,
	subscriptionTier: string | null,
	subscriptionExpiresAt: Date | null,
): string {
	const lines: string[] = ["💰 <b>Balance</b>\n"];

	lines.push(`<b>Credits:</b> ${userCredits}`);

	if (chatCredits > 0) {
		lines.push(`<b>Chat pool:</b> ${chatCredits}`);
	}

	if (subscriptionTier && subscriptionExpiresAt) {
		const isActive = subscriptionExpiresAt > new Date();
		if (isActive) {
			const pad = (n: number) => String(n).padStart(2, "0");
			const d = subscriptionExpiresAt;
			const expiry = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
			lines.push(
				`<b>Plan:</b> ${subscriptionTier.toUpperCase()} (renews ${expiry})`,
			);
		} else {
			lines.push("<b>Plan:</b> expired");
		}
	}

	return lines.join("\n");
}
