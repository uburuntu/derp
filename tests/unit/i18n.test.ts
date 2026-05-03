import { describe, expect, test } from "bun:test";
import type { DerpContext } from "../../src/bot/context";
import { getLocaleForContext, toSupportedLocale } from "../../src/i18n/index";

describe("toSupportedLocale", () => {
	test("normalizes supported locale variants", () => {
		expect(toSupportedLocale("ru-RU")).toBe("ru");
		expect(toSupportedLocale("en_US")).toBe("en");
	});

	test("ignores unsupported locales", () => {
		expect(toSupportedLocale("de")).toBeUndefined();
		expect(toSupportedLocale(null)).toBeUndefined();
	});
});

describe("getLocaleForContext", () => {
	test("prefers stored chat language over stored user language", () => {
		const ctx = {
			dbChat: { languageCode: "ru" },
			dbUser: { languageCode: "en" },
			from: { language_code: "en" },
		} as DerpContext;

		expect(getLocaleForContext(ctx)).toBe("ru");
	});

	test("falls back to stored user language before Telegram update language", () => {
		const ctx = {
			dbChat: { languageCode: null },
			dbUser: { languageCode: "ru-RU" },
			from: { language_code: "en" },
		} as DerpContext;

		expect(getLocaleForContext(ctx)).toBe("ru");
	});

	test("uses default locale for unsupported languages", () => {
		const ctx = {
			dbChat: { languageCode: null },
			dbUser: { languageCode: "de" },
			from: { language_code: "es" },
		} as DerpContext;

		expect(getLocaleForContext(ctx)).toBe("en");
	});
});
