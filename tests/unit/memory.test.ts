import { describe, expect, test } from "bun:test";
import {
    addChatMemoryItem,
    formatChatMemoryForPrompt,
    parseChatMemory,
    serializeChatMemory,
} from "../../src/memory/structured";
import { memoryTool } from "../../src/tools/memory";

describe("structured chat memory", () => {
    test("wraps legacy raw memory as a fact", () => {
        const doc = parseChatMemory("Alice prefers concise answers");

        expect(doc.facts[0]?.text).toBe("Alice prefers concise answers");
        expect(
            formatChatMemoryForPrompt("Alice prefers concise answers"),
        ).toContain("Facts:");
    });

    test("adds typed memory items and formats sections", () => {
        let raw = addChatMemoryItem(null, "fact", "Alice lives in London", {
            now: new Date("2026-01-01T00:00:00Z"),
        });
        raw = addChatMemoryItem(
            raw,
            "preference",
            "Alice wants short answers",
            {
                now: new Date("2026-01-02T00:00:00Z"),
            },
        );

        const formatted = formatChatMemoryForPrompt(raw);
        expect(formatted).toContain("Facts:");
        expect(formatted).toContain("Alice lives in London");
        expect(formatted).toContain("Preferences:");
        expect(formatted).toContain("Alice wants short answers");
    });

    test("deduplicates repeated items", () => {
        let raw = addChatMemoryItem(null, "topic", "launch checklist");
        raw = addChatMemoryItem(raw, "topic", "Launch checklist");

        expect(parseChatMemory(raw).topics).toHaveLength(1);
    });

    test("trims least important oldest items to fit the storage limit", () => {
        const doc = parseChatMemory(null);
        for (let i = 0; i < 10; i++) {
            doc.facts.push({
                text: `fact ${i} ${"x".repeat(100)}`,
                importance: i === 9 ? 5 : 1,
                updatedAt: new Date(2026, 0, i + 1).toISOString(),
            });
        }

        const serialized = serializeChatMemory(doc, 500);
        const trimmed = parseChatMemory(serialized);
        expect(serialized.length).toBeLessThanOrEqual(500);
        expect(
            trimmed.facts.some((item) => item.text.startsWith("fact 9")),
        ).toBe(true);
    });

    test("memory command parses typed prefixes", () => {
        expect(
            memoryTool.parseCommand?.(
                "preference: brief replies",
                "memory_set",
            ),
        ).toMatchObject({
            action: "update",
            kind: "preference",
            content: "brief replies",
        });
    });
});
