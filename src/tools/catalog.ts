import type { ToolCategory, ToolDefinition } from "./types";

export const CATEGORY_ORDER: ToolCategory[] = [
    "search",
    "reasoning",
    "media",
    "utility",
];

export const CATEGORY_LABELS: Record<ToolCategory, string> = {
    search: "Search & Research",
    reasoning: "Reasoning",
    media: "Media",
    utility: "Utilities",
};

export const CATEGORY_LABEL_KEYS: Record<ToolCategory, string> = {
    search: "tool-category-search",
    reasoning: "tool-category-reasoning",
    media: "tool-category-media",
    utility: "tool-category-utility",
};

export const CATEGORY_EMOJI: Record<ToolCategory, string> = {
    search: "🔍",
    reasoning: "🧠",
    media: "🎨",
    utility: "🛠",
};

export type Translator = (
    key: string,
    args?: Record<string, string | number>,
) => string;

export function primaryCommand(tool: ToolDefinition): string {
    return tool.commands[0] ?? tool.name;
}

export function formatToolCost(tool: ToolDefinition, t?: Translator): string {
    const hasFiniteQuota =
        Number.isFinite(tool.freeDaily) && tool.freeDaily > 0;
    if (tool.credits === 0) {
        if (!hasFiniteQuota) return t ? t("tool-cost-free") : "free";
        return t
            ? t("tool-cost-free-daily", { freeDaily: tool.freeDaily })
            : `${tool.freeDaily} free per user/chat/day`;
    }

    if (!hasFiniteQuota) {
        return t
            ? t("tool-cost-credits", { credits: tool.credits })
            : `${tool.credits} cr`;
    }

    return t
        ? t("tool-cost-credits-with-quota", {
              credits: tool.credits,
              freeDaily: tool.freeDaily,
          })
        : `${tool.credits} cr, ${tool.freeDaily} free per user/chat/day`;
}
