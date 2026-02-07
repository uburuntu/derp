/** Video generation tool — generates short videos from text prompts */

import { z } from "zod";
import { config, getGoogleApiKeys } from "../config";
import { GoogleLLMProvider } from "../llm/providers/google";
import { ModelCapability } from "../llm/registry";
import type { ToolContext, ToolDefinition, ToolResult } from "./types";

const videoParamsSchema = z.object({
	prompt: z
		.string()
		.describe("A detailed description of the video to generate"),
});

type VideoParams = z.infer<typeof videoParamsSchema>;

async function executeVideo(
	params: VideoParams,
	ctx: ToolContext,
): Promise<ToolResult> {
	// Send progress message
	await ctx.sendMessage(
		"Generating video... This may take a couple of minutes.",
	);

	const provider = new GoogleLLMProvider(
		getGoogleApiKeys(config),
		config.googleApiPaidKey,
	);

	try {
		const result = await provider.generateVideo({
			model: "veo-3.1-fast-generate-preview",
			prompt: params.prompt,
			timeoutMs: 180_000,
		});

		await ctx.sendVideo(result.video.data, params.prompt);
		return { handled: true };
	} catch (err) {
		const msg = err instanceof Error ? err.message : String(err);
		return { text: `Video generation failed: ${msg}`, error: msg };
	}
}

export const videoTool: ToolDefinition<VideoParams> = {
	name: "video",
	commands: ["/video", "/v"],
	description: "Generate a short 5-second video from a text description",
	helpText: "tool-video",
	category: "media",
	parameters: videoParamsSchema,
	execute: executeVideo,
	credits: 250,
	freeDaily: 0,
	capability: ModelCapability.VIDEO,
	defaultModel: "veo-3.1-fast-generate-preview",
};
