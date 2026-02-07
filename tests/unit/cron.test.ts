import { describe, expect, test } from "bun:test";
import { parseCronToNextDate, validateCron } from "../../src/scheduler/cron";

describe("parseCronToNextDate", () => {
	test("parses simple cron expression", () => {
		// Every day at midnight
		const next = parseCronToNextDate("0 0 * * *");
		expect(next).not.toBeNull();
		expect(next!.getHours()).toBe(0);
		expect(next!.getMinutes()).toBe(0);
	});

	test("returns future date", () => {
		const next = parseCronToNextDate("* * * * *"); // Every minute
		expect(next).not.toBeNull();
		expect(next!.getTime()).toBeGreaterThan(Date.now());
	});

	test("handles weekday filter", () => {
		// Weekdays only (Mon-Fri = 1-5)
		const next = parseCronToNextDate("0 9 * * 1-5");
		expect(next).not.toBeNull();
		const day = next!.getDay();
		expect(day).toBeGreaterThanOrEqual(1);
		expect(day).toBeLessThanOrEqual(5);
	});

	test("returns null for invalid expression", () => {
		expect(parseCronToNextDate("invalid")).toBeNull();
		expect(parseCronToNextDate("1 2 3")).toBeNull();
	});

	test("handles step syntax", () => {
		// Every 15 minutes
		const next = parseCronToNextDate("*/15 * * * *");
		expect(next).not.toBeNull();
		expect(next!.getMinutes() % 15).toBe(0);
	});

	test("handles comma-separated values", () => {
		// At 9am and 5pm
		const next = parseCronToNextDate("0 9,17 * * *");
		expect(next).not.toBeNull();
		const hour = next!.getHours();
		expect(hour === 9 || hour === 17).toBe(true);
	});
});

describe("validateCron", () => {
	test("accepts valid cron", () => {
		expect(validateCron("0 9 * * 1-5")).toBeNull();
		expect(validateCron("*/15 * * * *")).toBeNull();
		expect(validateCron("0 0 1 * *")).toBeNull();
	});

	test("rejects wrong number of fields", () => {
		const err = validateCron("1 2 3");
		expect(err).not.toBeNull();
		expect(err).toContain("5 fields");
	});
});
