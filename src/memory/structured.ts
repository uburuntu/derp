import { z } from "zod";

export const STRUCTURED_MEMORY_VERSION = 1;
export const MAX_MEMORY_LENGTH = 4096;

export type ChatMemoryKind = "fact" | "preference" | "topic";

export interface ChatMemoryItem {
    text: string;
    importance: number;
    updatedAt: string;
}

export interface ChatMemoryDocument {
    version: typeof STRUCTURED_MEMORY_VERSION;
    facts: ChatMemoryItem[];
    preferences: ChatMemoryItem[];
    topics: ChatMemoryItem[];
}

const MAX_ITEM_TEXT_LENGTH = 280;
const SECTION_LABELS: Record<
    keyof Omit<ChatMemoryDocument, "version">,
    string
> = {
    facts: "Facts",
    preferences: "Preferences",
    topics: "Open topics",
};

const memoryItemSchema = z.object({
    text: z.string(),
    importance: z.number().int().min(1).max(5).default(1),
    updatedAt: z.string(),
});

const memoryDocumentSchema = z.object({
    version: z.literal(STRUCTURED_MEMORY_VERSION),
    facts: z.array(memoryItemSchema).default([]),
    preferences: z.array(memoryItemSchema).default([]),
    topics: z.array(memoryItemSchema).default([]),
});

function emptyMemoryDocument(): ChatMemoryDocument {
    return {
        version: STRUCTURED_MEMORY_VERSION,
        facts: [],
        preferences: [],
        topics: [],
    };
}

function normalizeText(text: string): string {
    return text.replace(/\s+/g, " ").trim().slice(0, MAX_ITEM_TEXT_LENGTH);
}

function keyForKind(
    kind: ChatMemoryKind,
): keyof Omit<ChatMemoryDocument, "version"> {
    switch (kind) {
        case "preference":
            return "preferences";
        case "topic":
            return "topics";
        default:
            return "facts";
    }
}

function cloneDocument(doc: ChatMemoryDocument): ChatMemoryDocument {
    return {
        version: STRUCTURED_MEMORY_VERSION,
        facts: doc.facts.map((item) => ({ ...item })),
        preferences: doc.preferences.map((item) => ({ ...item })),
        topics: doc.topics.map((item) => ({ ...item })),
    };
}

export function parseChatMemory(
    raw: string | null | undefined,
): ChatMemoryDocument {
    if (!raw?.trim()) return emptyMemoryDocument();

    try {
        const parsed = memoryDocumentSchema.safeParse(JSON.parse(raw));
        if (parsed.success) return parsed.data;
    } catch {
        // Legacy memory was plain text. Keep it readable as one fact.
    }

    const legacy = normalizeText(raw);
    if (!legacy) return emptyMemoryDocument();
    return {
        ...emptyMemoryDocument(),
        facts: [
            {
                text: legacy,
                importance: 2,
                updatedAt: new Date(0).toISOString(),
            },
        ],
    };
}

export function serializeChatMemory(
    doc: ChatMemoryDocument,
    maxLength = MAX_MEMORY_LENGTH,
): string {
    const trimmed = trimMemoryDocument(doc, maxLength);
    return JSON.stringify(trimmed);
}

export function addChatMemoryItem(
    raw: string | null | undefined,
    kind: ChatMemoryKind,
    text: string,
    options: { importance?: number; now?: Date } = {},
): string {
    const normalized = normalizeText(text);
    const doc = parseChatMemory(raw);
    if (!normalized) return serializeChatMemory(doc);

    const key = keyForKind(kind);
    const importance = Math.max(1, Math.min(5, options.importance ?? 3));
    const updatedAt = (options.now ?? new Date()).toISOString();
    const existing = doc[key].find(
        (item) => item.text.toLowerCase() === normalized.toLowerCase(),
    );

    if (existing) {
        existing.importance = Math.max(existing.importance, importance);
        existing.updatedAt = updatedAt;
    } else {
        doc[key].push({ text: normalized, importance, updatedAt });
    }

    return serializeChatMemory(doc);
}

export function formatChatMemoryForPrompt(
    raw: string | null | undefined,
): string {
    const doc = parseChatMemory(raw);
    return formatDocument(doc);
}

export function formatChatMemoryForDisplay(
    raw: string | null | undefined,
): string {
    const formatted = formatChatMemoryForPrompt(raw);
    return formatted || "No memory stored for this chat yet.";
}

function formatDocument(doc: ChatMemoryDocument): string {
    const sections: string[] = [];
    for (const key of ["facts", "preferences", "topics"] as const) {
        const items = doc[key];
        if (items.length === 0) continue;
        const body = items
            .slice()
            .sort(compareImportantFirst)
            .map((item) => `- ${item.text}`)
            .join("\n");
        sections.push(`${SECTION_LABELS[key]}:\n${body}`);
    }
    return sections.join("\n\n");
}

function compareImportantFirst(a: ChatMemoryItem, b: ChatMemoryItem): number {
    return (
        b.importance - a.importance ||
        Date.parse(b.updatedAt) - Date.parse(a.updatedAt)
    );
}

function trimMemoryDocument(
    doc: ChatMemoryDocument,
    maxLength: number,
): ChatMemoryDocument {
    const trimmed = cloneDocument(doc);
    trimItems(trimmed);

    while (JSON.stringify(trimmed).length > maxLength) {
        const victim = leastValuableItem(trimmed);
        if (!victim) break;
        trimmed[victim.key].splice(victim.index, 1);
    }

    return trimmed;
}

function trimItems(doc: ChatMemoryDocument): void {
    for (const key of ["facts", "preferences", "topics"] as const) {
        doc[key] = doc[key]
            .map((item) => ({
                text: normalizeText(item.text),
                importance: Math.max(
                    1,
                    Math.min(5, Math.round(item.importance)),
                ),
                updatedAt: Number.isNaN(Date.parse(item.updatedAt))
                    ? new Date(0).toISOString()
                    : item.updatedAt,
            }))
            .filter((item) => item.text.length > 0);
    }
}

function leastValuableItem(
    doc: ChatMemoryDocument,
): { key: "facts" | "preferences" | "topics"; index: number } | null {
    const candidates: Array<{
        key: "facts" | "preferences" | "topics";
        index: number;
        item: ChatMemoryItem;
    }> = [];

    for (const key of ["facts", "preferences", "topics"] as const) {
        doc[key].forEach((item, index) => {
            candidates.push({ key, index, item });
        });
    }

    candidates.sort(
        (a, b) =>
            a.item.importance - b.item.importance ||
            Date.parse(a.item.updatedAt) - Date.parse(b.item.updatedAt),
    );

    return candidates[0] ?? null;
}
