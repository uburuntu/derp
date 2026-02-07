import { describe, expect, test } from "bun:test";
import { getTopUpPack, TOPUP_PACKS } from "../../src/credits/packs";
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
		expect(lite).toBeDefined();
		expect(lite!.stars).toBe(150);
		expect(lite!.credits).toBe(200);
	});

	test("Pro plan: 500 stars, 750 credits, POPULAR tag", () => {
		const pro = getSubscriptionPlan("pro");
		expect(pro).toBeDefined();
		expect(pro!.stars).toBe(500);
		expect(pro!.credits).toBe(750);
		expect(pro!.tag).toBe("POPULAR");
	});

	test("Ultra plan: 1500 stars, 2500 credits, BEST VALUE tag", () => {
		const ultra = getSubscriptionPlan("ultra");
		expect(ultra).toBeDefined();
		expect(ultra!.stars).toBe(1500);
		expect(ultra!.credits).toBe(2500);
		expect(ultra!.tag).toBe("BEST VALUE");
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
		expect(micro).toBeDefined();
		expect(micro!.stars).toBe(50);
		expect(micro!.credits).toBe(50);
	});

	test("Small pack: 150 stars, 150 credits", () => {
		const small = getTopUpPack("small");
		expect(small).toBeDefined();
		expect(small!.stars).toBe(150);
		expect(small!.credits).toBe(150);
	});

	test("Medium pack: 500 stars, 550 credits, +10% bonus", () => {
		const medium = getTopUpPack("medium");
		expect(medium).toBeDefined();
		expect(medium!.stars).toBe(500);
		expect(medium!.credits).toBe(550);
		expect(medium!.bonus).toBe("+10%");
	});

	test("Large pack: 1500 stars, 1800 credits, +20% bonus", () => {
		const large = getTopUpPack("large");
		expect(large).toBeDefined();
		expect(large!.stars).toBe(1500);
		expect(large!.credits).toBe(1800);
		expect(large!.bonus).toBe("+20%");
	});

	test("returns undefined for unknown pack", () => {
		expect(getTopUpPack("nonexistent")).toBeUndefined();
	});
});
