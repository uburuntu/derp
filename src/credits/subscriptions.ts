/** Subscription plan definitions — exact values from PRD SS7.4 */

export interface SubscriptionPlan {
	id: string;
	label: string;
	stars: number;
	credits: number;
	savings: string;
	tag?: string;
}

export const SUBSCRIPTION_PLANS: SubscriptionPlan[] = [
	{ id: "lite", label: "Lite", stars: 150, credits: 200, savings: "25%" },
	{
		id: "pro",
		label: "Pro",
		stars: 500,
		credits: 750,
		savings: "33%",
		tag: "POPULAR",
	},
	{
		id: "ultra",
		label: "Ultra",
		stars: 1500,
		credits: 2500,
		savings: "40%",
		tag: "BEST VALUE",
	},
];

export function getSubscriptionPlan(id: string): SubscriptionPlan | undefined {
	return SUBSCRIPTION_PLANS.find((p) => p.id === id);
}
