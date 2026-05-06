import { toJSONSchema, type z } from "zod";
import { primaryCommand } from "./catalog";
import type { ToolDefinition } from "./types";

interface ParsedToolParams {
    ok: true;
    params: unknown;
}

interface ToolUsageError {
    ok: false;
    usage: string;
}

export type ToolParseResult = ParsedToolParams | ToolUsageError;

export function zodToJsonSchema(schema: z.ZodSchema): Record<string, unknown> {
    return toJSONSchema(schema) as Record<string, unknown>;
}

export function parseToolParams(
    tool: ToolDefinition,
    input: string,
    command?: string,
): ToolParseResult {
    const schemaShape = tool.parameters;

    try {
        const jsonSchema = zodToJsonSchema(schemaShape);
        const properties = (jsonSchema as Record<string, unknown>).properties as
            | Record<string, unknown>
            | undefined;
        const required = (jsonSchema as Record<string, unknown>).required as
            | string[]
            | undefined;

        let params: unknown;
        if (tool.parseCommand) {
            params = tool.parseCommand(input, command);
        } else if (properties && required && required.length > 0) {
            const firstField = required[0];
            params = firstField ? { [firstField]: input } : {};
        } else {
            params = { query: input };
        }

        const parsed = schemaShape.safeParse(params);
        if (!parsed.success) {
            const usage = `Usage: ${tool.usage ?? `${primaryCommand(tool)} <${required?.[0] ?? "input"}>`}`;
            return { ok: false, usage };
        }

        return { ok: true, params: parsed.data };
    } catch {
        return {
            ok: false,
            usage: `Usage: ${tool.usage ?? `${primaryCommand(tool)} <input>`}`,
        };
    }
}
