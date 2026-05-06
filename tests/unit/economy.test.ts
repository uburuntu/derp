import { describe, expect, test } from "bun:test";
import {
	creditUsdFloor,
	providerCostToCredits,
} from "../../src/credits/economy";
import {
	STANDARD_CHAT_CREDITS,
	STANDARD_CHAT_TOOL_NAME,
} from "../../src/credits/service";
import {
	getDefaultModel,
	getModel,
	ModelCapability,
	ModelTier,
} from "../../src/llm/registry";
import { loadToolDefinitions } from "../../src/tools/loader";

describe("credit economy", () => {
	test("uses cheapest sold credit as pricing floor", () => {
		expect(creditUsdFloor()).toBeCloseTo(0.0078, 6);
		expect(providerCostToCredits(0.039)).toBe(8);
	});

	test("paid tools cover their default model floor", async () => {
		const tools = await loadToolDefinitions();
		for (const tool of tools) {
			if (tool.credits <= 0 || !tool.defaultModel) continue;
			expect(tool.credits).toBeGreaterThanOrEqual(
				getModel(tool.defaultModel).creditCost,
			);
		}
	});

	test("standard chat has an explicit metered price", () => {
		const model = getDefaultModel(ModelCapability.TEXT, ModelTier.STANDARD);
		expect(STANDARD_CHAT_TOOL_NAME).toBe("chat");
		expect(STANDARD_CHAT_CREDITS).toBeGreaterThanOrEqual(model.creditCost);
	});
});
