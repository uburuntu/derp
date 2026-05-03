import { describe, expect, test } from "bun:test";
import {
	CONTEXT_LIMITS,
	getAllModels,
	getDefaultModel,
	getModel,
	ModelCapability,
	ModelTier,
} from "../../src/llm/registry";

describe("ModelRegistry", () => {
	test("FREE context limit is 15", () => {
		expect(CONTEXT_LIMITS[ModelTier.FREE]).toBe(15);
	});

	test("STANDARD context limit is 100", () => {
		expect(CONTEXT_LIMITS[ModelTier.STANDARD]).toBe(100);
	});

	test("PREMIUM context limit exists for registry completeness", () => {
		expect(CONTEXT_LIMITS[ModelTier.PREMIUM]).toBe(100);
	});

	test("getModel returns correct model", () => {
		const model = getModel("gemini-2.5-flash");
		expect(model.id).toBe("gemini-2.5-flash");
		expect(model.tier).toBe(ModelTier.STANDARD);
		expect(model.capability).toBe(ModelCapability.TEXT);
	});

	test("getModel throws for unknown model", () => {
		expect(() => getModel("nonexistent")).toThrow("Unknown model");
	});

	test("getDefaultModel returns FREE text model", () => {
		const model = getDefaultModel(ModelCapability.TEXT, ModelTier.FREE);
		expect(model.id).toBe("gemini-2.5-flash-lite");
	});

	test("getDefaultModel returns STANDARD text model", () => {
		const model = getDefaultModel(ModelCapability.TEXT, ModelTier.STANDARD);
		expect(model.id).toBe("gemini-2.5-flash");
	});

	test("image model ID is gemini-2.5-flash-preview-image", () => {
		const model = getDefaultModel(ModelCapability.IMAGE, ModelTier.STANDARD);
		expect(model.id).toBe("gemini-2.5-flash-preview-image");
	});

	test("video model ID is veo-3.1-fast-generate-preview", () => {
		const model = getDefaultModel(ModelCapability.VIDEO, ModelTier.STANDARD);
		expect(model.id).toBe("veo-3.1-fast-generate-preview");
	});

	test("TTS model ID is gemini-2.5-pro-preview-tts", () => {
		const model = getDefaultModel(ModelCapability.VOICE, ModelTier.STANDARD);
		expect(model.id).toBe("gemini-2.5-pro-preview-tts");
	});

	test("getAllModels returns 6 models", () => {
		expect(getAllModels().length).toBe(6);
	});

	test("gemini-3-pro-preview is registered under PREMIUM", () => {
		const model = getModel("gemini-3-pro-preview");
		expect(model.tier).toBe(ModelTier.PREMIUM);
	});
});
