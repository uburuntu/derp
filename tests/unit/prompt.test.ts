import { describe, expect, test } from "bun:test";
import { buildSystemPrompt, detectTaskSpecialist } from "../../src/llm/prompt";

describe("buildSystemPrompt", () => {
    test("uses default personality when no override", () => {
        const prompt = buildSystemPrompt("default", null, null);
        expect(prompt).toContain("You are Derp");
        expect(prompt).toContain("conversational");
    });

    test("uses professional personality", () => {
        const prompt = buildSystemPrompt("professional", null, null);
        expect(prompt).toContain("professional");
        expect(prompt).toContain("formal");
    });

    test("uses casual personality", () => {
        const prompt = buildSystemPrompt("casual", null, null);
        expect(prompt).toContain("chill");
    });

    test("uses creative personality", () => {
        const prompt = buildSystemPrompt("creative", null, null);
        expect(prompt).toContain("creative");
        expect(prompt).toContain("imaginative");
    });

    test("uses custom prompt when personality is 'custom'", () => {
        const prompt = buildSystemPrompt("custom", "You are a pirate.", null);
        expect(prompt).toContain("You are a pirate.");
        expect(prompt).not.toContain("You are Derp");
    });

    test("falls back to default when custom is selected but no prompt", () => {
        const prompt = buildSystemPrompt("custom", null, null);
        expect(prompt).toContain("You are Derp");
    });

    test("always includes core rules", () => {
        const prompt = buildSystemPrompt("default", null, null);
        expect(prompt).toContain("## Rules");
        expect(prompt).toContain("standard Markdown");
        expect(prompt).toContain("200 words");
    });

    test("includes memory when provided", () => {
        const prompt = buildSystemPrompt("default", null, "User likes cats");
        expect(prompt).toContain("## Chat Memory");
        expect(prompt).toContain("User likes cats");
    });

    test("does not include memory section when null", () => {
        const prompt = buildSystemPrompt("default", null, null);
        expect(prompt).not.toContain("Chat Memory");
    });

    test("includes user preferences when provided", () => {
        const prompt = buildSystemPrompt("default", null, null, {
            responseStyle: "concise",
            customInstructions: "I prefer TypeScript examples.",
        });
        expect(prompt).toContain("## User Preferences");
        expect(prompt).toContain("Prefer concise answers");
        expect(prompt).toContain("I prefer TypeScript examples.");
    });

    test("detects task specialists", () => {
        expect(detectTaskSpecialist("This TypeScript test fails")).toBe("code");
        expect(
            detectTaskSpecialist("research the latest market evidence"),
        ).toBe("research");
        expect(detectTaskSpecialist("rewrite this brand slogan")).toBe(
            "creative",
        );
        expect(detectTaskSpecialist("hello there")).toBe("general");
    });
});
