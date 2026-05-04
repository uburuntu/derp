import { describe, expect, test } from "bun:test";
import {
	isToolDisabled,
	normalizeUserPreferences,
	toggleDisabledTool,
} from "../../src/preferences/user";

describe("user preferences", () => {
	test("normalizes disabled tools deterministically", () => {
		const prefs = normalizeUserPreferences({
			disabledTools: ["video", "webSearch", "video"],
		});

		expect(prefs.disabledTools).toEqual(["video", "webSearch"]);
	});

	test("toggles disabled tools", () => {
		const disabled = toggleDisabledTool(null, "video");
		expect(disabled).toEqual({ disabledTools: ["video"], disabled: true });
		expect(isToolDisabled({ disabledTools: ["video"] }, "video")).toBe(true);

		const enabled = toggleDisabledTool({ disabledTools: ["video"] }, "video");
		expect(enabled).toEqual({ disabledTools: [], disabled: false });
	});
});
