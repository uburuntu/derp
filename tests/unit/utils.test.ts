import { describe, expect, test } from "bun:test";
import {
	normalizeWhitespace,
	removeInvisible,
	truncate,
} from "../../src/common/sanitize";
import {
	displayName,
	displayNameFromDb,
	escapeMarkdownV2,
} from "../../src/common/telegram";

describe("displayName", () => {
	test("returns first name only", () => {
		expect(
			displayName({
				id: 1,
				is_bot: false,
				first_name: "Alice",
			}),
		).toBe("Alice");
	});

	test("returns first + last name", () => {
		expect(
			displayName({
				id: 1,
				is_bot: false,
				first_name: "Alice",
				last_name: "Smith",
			}),
		).toBe("Alice Smith");
	});
});

describe("displayNameFromDb", () => {
	test("returns first name when no last name", () => {
		expect(displayNameFromDb("Alice", null)).toBe("Alice");
	});

	test("returns full name", () => {
		expect(displayNameFromDb("Alice", "Smith")).toBe("Alice Smith");
	});
});

describe("escapeMarkdownV2", () => {
	test("escapes special characters", () => {
		expect(escapeMarkdownV2("hello_world")).toBe("hello\\_world");
		expect(escapeMarkdownV2("test*bold*")).toBe("test\\*bold\\*");
		expect(escapeMarkdownV2("price: $10.00")).toBe("price: $10\\.00");
	});

	test("passes through plain text", () => {
		expect(escapeMarkdownV2("hello world")).toBe("hello world");
	});
});

describe("truncate", () => {
	test("returns text unchanged if within limit", () => {
		expect(truncate("hello", 10)).toBe("hello");
	});

	test("truncates with ellipsis", () => {
		const result = truncate("hello world this is long", 10);
		expect(result.length).toBe(10);
		expect(result.endsWith("…")).toBe(true);
	});
});

describe("normalizeWhitespace", () => {
	test("collapses multiple spaces", () => {
		expect(normalizeWhitespace("hello   world")).toBe("hello world");
	});

	test("collapses excessive newlines", () => {
		expect(normalizeWhitespace("a\n\n\n\nb")).toBe("a\n\nb");
	});

	test("trims", () => {
		expect(normalizeWhitespace("  hello  ")).toBe("hello");
	});
});

describe("removeInvisible", () => {
	test("removes zero-width characters", () => {
		expect(removeInvisible("hello\u200Bworld")).toBe("helloworld");
		expect(removeInvisible("test\uFEFFdata")).toBe("testdata");
	});

	test("keeps normal text", () => {
		expect(removeInvisible("hello world")).toBe("hello world");
	});
});
