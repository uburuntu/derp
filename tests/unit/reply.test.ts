import { describe, expect, test } from "bun:test";
import {
	appendFooterToChunks,
	formatBalanceFooter,
	needsCaptionOverflow,
	splitCaption,
	splitMessage,
} from "../../src/common/reply";

describe("splitMessage", () => {
	test("returns single chunk for short messages", () => {
		const result = splitMessage("Hello world");
		expect(result).toEqual(["Hello world"]);
	});

	test("splits on paragraph boundary", () => {
		const text = "A".repeat(3000) + "\n\n" + "B".repeat(2000);
		const result = splitMessage(text);
		expect(result.length).toBe(2);
		expect(result[0]!.endsWith("\n\n")).toBe(true);
	});

	test("splits on sentence boundary when no paragraph break", () => {
		const text = "A".repeat(3000) + ". " + "B".repeat(2000);
		const result = splitMessage(text);
		expect(result.length).toBe(2);
	});

	test("hard splits when no boundaries found", () => {
		const text = "A".repeat(5000);
		const result = splitMessage(text);
		expect(result.length).toBe(2);
		expect(result[0]!.length).toBe(4096);
	});

	test("respects custom max length", () => {
		const text = "Hello world, this is a test";
		const result = splitMessage(text, 10);
		expect(result.length).toBeGreaterThan(1);
	});
});

describe("formatBalanceFooter", () => {
	test("returns empty for zero cost", () => {
		expect(formatBalanceFooter(0, 100)).toBe("");
	});

	test("shows warning when remaining <= 20", () => {
		const footer = formatBalanceFooter(10, 15);
		expect(footer).toContain("⚠️");
		expect(footer).toContain("/buy");
	});

	test("shows normal format when remaining > 20", () => {
		const footer = formatBalanceFooter(5, 100);
		expect(footer).toContain("✨");
		expect(footer).not.toContain("/buy");
	});
});

describe("appendFooterToChunks", () => {
	test("does nothing for zero cost", () => {
		const result = appendFooterToChunks(["Hello"], 0, 100);
		expect(result).toEqual(["Hello"]);
	});

	test("appends footer to last chunk", () => {
		const result = appendFooterToChunks(["Hello"], 5, 100);
		expect(result[result.length - 1]).toContain("5 credits used");
	});
});

describe("needsCaptionOverflow", () => {
	test("returns false for short captions", () => {
		expect(needsCaptionOverflow("Short caption")).toBe(false);
	});

	test("returns true for long captions", () => {
		expect(needsCaptionOverflow("A".repeat(1025))).toBe(true);
	});
});

describe("splitCaption", () => {
	test("returns full caption when short", () => {
		const result = splitCaption("Short caption");
		expect(result.mediaCaption).toBe("Short caption");
		expect(result.followUp).toBe("");
	});

	test("truncates long captions", () => {
		const longCaption = "A".repeat(2000);
		const result = splitCaption(longCaption);
		expect(result.mediaCaption.length).toBeLessThanOrEqual(1024);
		expect(result.followUp).toBe(longCaption);
	});
});
