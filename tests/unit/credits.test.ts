import { describe, expect, test } from "bun:test";
import { getTopUpPack, TOPUP_PACKS } from "../../src/credits/packs";
import { hasActiveSubscription } from "../../src/credits/service";
import {
	getSubscriptionPlan,
	SUBSCRIPTION_PLANS,
} from "../../src/credits/subscriptions";

describe("Subscription Plans", () => {
	test("has 3 plans", () => {
		expect(SUBSCRIPTION_PLANS.length).toBe(3);
	});

	test("Lite plan: 150 stars, 200 credits", () => {
		const lite = getSubscriptionPlan("lite");
		expect(lite).toEqual(expect.objectContaining({ stars: 150, credits: 200 }));
	});

	test("Pro plan: 500 stars, 750 credits, POPULAR tag", () => {
		const pro = getSubscriptionPlan("pro");
		expect(pro).toEqual(
			expect.objectContaining({ stars: 500, credits: 750, tag: "POPULAR" }),
		);
	});

	test("Ultra plan: 1500 stars, 2500 credits, BEST VALUE tag", () => {
		const ultra = getSubscriptionPlan("ultra");
		expect(ultra).toEqual(
			expect.objectContaining({
				stars: 1500,
				credits: 2500,
				tag: "BEST VALUE",
			}),
		);
	});

	test("returns undefined for unknown plan", () => {
		expect(getSubscriptionPlan("nonexistent")).toBeUndefined();
	});
});

describe("Top-Up Packs", () => {
	test("has 4 packs", () => {
		expect(TOPUP_PACKS.length).toBe(4);
	});

	test("Micro pack: 50 stars, 50 credits", () => {
		const micro = getTopUpPack("micro");
		expect(micro).toEqual(expect.objectContaining({ stars: 50, credits: 50 }));
	});

	test("Small pack: 150 stars, 150 credits", () => {
		const small = getTopUpPack("small");
		expect(small).toEqual(
			expect.objectContaining({ stars: 150, credits: 150 }),
		);
	});

	test("Medium pack: 500 stars, 550 credits, +10% bonus", () => {
		const medium = getTopUpPack("medium");
		expect(medium).toEqual(
			expect.objectContaining({ stars: 500, credits: 550, bonus: "+10%" }),
		);
	});

	test("Large pack: 1500 stars, 1800 credits, +20% bonus", () => {
		const large = getTopUpPack("large");
		expect(large).toEqual(
			expect.objectContaining({
				stars: 1500,
				credits: 1800,
				bonus: "+20%",
			}),
		);
	});

	test("returns undefined for unknown pack", () => {
		expect(getTopUpPack("nonexistent")).toBeUndefined();
	});
});

describe("Subscription state", () => {
	test("requires a non-expired subscription", () => {
		expect(
			hasActiveSubscription({
				subscriptionTier: "pro",
				subscriptionExpiresAt: new Date(Date.now() + 60_000),
			}),
		).toBe(true);

		expect(
			hasActiveSubscription({
				subscriptionTier: "pro",
				subscriptionExpiresAt: new Date(Date.now() - 60_000),
			}),
		).toBe(false);
	});
});
