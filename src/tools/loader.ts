/** Tool loader — discovers exported ToolDefinition objects from src/tools/*.ts */

import { readdir } from "node:fs/promises";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { logger } from "../common/observability";
import type { ToolDefinition } from "./types";

const NON_TOOL_MODULES = new Set([
    "credit-gate.ts",
    "loader.ts",
    "registry.ts",
    "types.ts",
]);

function isToolDefinition(value: unknown): value is ToolDefinition {
    if (!value || typeof value !== "object") return false;

    const candidate = value as Partial<ToolDefinition>;
    return (
        typeof candidate.name === "string" &&
        Array.isArray(candidate.commands) &&
        typeof candidate.description === "string" &&
        typeof candidate.helpText === "string" &&
        typeof candidate.category === "string" &&
        typeof candidate.execute === "function" &&
        typeof candidate.credits === "number" &&
        typeof candidate.freeDaily === "number" &&
        candidate.parameters != null
    );
}

export async function loadToolDefinitions(): Promise<ToolDefinition[]> {
    const entries = (await readdir(import.meta.dir))
        .filter(
            (entry) => entry.endsWith(".ts") && !NON_TOOL_MODULES.has(entry),
        )
        .sort();

    const tools: ToolDefinition[] = [];
    for (const entry of entries) {
        const modulePath = pathToFileURL(join(import.meta.dir, entry)).href;
        const mod = await import(modulePath);
        for (const exported of Object.values(mod)) {
            if (isToolDefinition(exported)) {
                tools.push(exported);
            }
        }
    }

    logger.info("tools_discovered", {
        count: tools.length,
        tools: tools.map((tool) => tool.name),
    });
    return tools;
}
