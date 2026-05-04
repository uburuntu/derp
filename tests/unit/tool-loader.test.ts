import { describe, expect, test } from "bun:test";
import { loadToolDefinitions } from "../../src/tools/loader";

describe("tool loader", () => {
	test("discovers exported tool definitions only", async () => {
		const tools = await loadToolDefinitions();
		const names = tools.map((tool) => tool.name).sort();

		expect(names).toEqual([
			"editImage",
			"getMember",
			"imagine",
			"memory",
			"remind",
			"think",
			"tts",
			"video",
			"webSearch",
		]);
	});
});
