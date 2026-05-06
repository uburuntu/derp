/** Deep reasoning tool — extended thinking with Gemini 3 Pro */

import { z } from "zod";
import { config, getGoogleApiKeys } from "../config";
import { GoogleLLMProvider } from "../llm/providers/google";
import { ModelCapability } from "../llm/registry";
import type { ToolContext, ToolDefinition, ToolResult } from "./types";

const thinkParamsSchema = z.object({
	question: z
		.string()
		.max(12_000)
		.describe(
			"The question or problem to think deeply about. Provide full context.",
		),
});

type ThinkParams = z.infer<typeof thinkParamsSchema>;

async function executeThink(
	params: ThinkParams,
	ctx: ToolContext,
): Promise<ToolResult> {
	const provider = new GoogleLLMProvider(
		getGoogleApiKeys(config),
		config.googleApiPaidKey,
	);

	try {
		const result = await provider.chat({
			model: "gemini-3.1-pro-preview",
			systemPrompt:
				"You are a deep reasoning assistant. Think step by step. Be thorough and precise.",
			messages: [{ role: "user", content: params.question }],
			maxOutputTokens: 4096,
			timeoutMs: 60_000,
			tracking: {
				db: ctx.db,
				logicalRequestKey: ctx.idempotencyKey,
				operation: "think",
				keyClass: "paid",
				userId: ctx.user.id,
				chatId: ctx.chat.id,
				toolName: "think",
				creditsCharged: ctx.creditResult?.creditsToDeduct ?? 0,
				creditSource: ctx.creditResult?.source,
			},
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
	credits: 20,
	freeDaily: 0,
	capability: ModelCapability.TEXT,
	defaultModel: "gemini-3.1-pro-preview",
};
