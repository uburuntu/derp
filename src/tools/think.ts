/** Deep reasoning tool — extended thinking with Gemini 3 Pro */

import { z } from "zod";
import { config, getGoogleApiKeys } from "../config";
import { GoogleLLMProvider } from "../llm/providers/google";
import { ModelCapability } from "../llm/registry";
import type { ToolContext, ToolDefinition, ToolResult } from "./types";

const thinkParamsSchema = z.object({
	question: z
		.string()
		.describe(
			"The question or problem to think deeply about. Provide full context.",
		),
});

type ThinkParams = z.infer<typeof thinkParamsSchema>;

async function executeThink(
	params: ThinkParams,
	_ctx: ToolContext,
): Promise<ToolResult> {
	const provider = new GoogleLLMProvider(
		getGoogleApiKeys(config),
		config.googleApiPaidKey,
	);

	try {
		const result = await provider.chat({
			model: "gemini-3-pro-preview",
			systemPrompt:
				"You are a deep reasoning assistant. Think step by step. Be thorough and precise.",
			messages: [{ role: "user", content: params.question }],
			maxOutputTokens: 8192,
			timeoutMs: 60_000,
		});

		return { text: result.text };
	} catch (err) {
		const msg = err instanceof Error ? err.message : String(err);
		return { text: `Deep reasoning failed: ${msg}`, error: msg };
	}
}

export const thinkTool: ToolDefinition<ThinkParams> = {
	name: "think",
	commands: ["/think", "/t"],
	description:
		"Deep reasoning — think step-by-step about complex problems using an advanced model",
	helpText: "tool-think",
	category: "reasoning",
	parameters: thinkParamsSchema,
	execute: executeThink,
	credits: 5,
	freeDaily: 0,
	capability: ModelCapability.TEXT,
	defaultModel: "gemini-3-pro-preview",
};
