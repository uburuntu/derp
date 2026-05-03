/** Image generation tool — generates images from text prompts */

import { z } from "zod";
import { captionPartsForMedia } from "../common/reply";
import { config, getGoogleApiKeys } from "../config";
import { GoogleLLMProvider } from "../llm/providers/google";
import { ModelCapability } from "../llm/registry";
import type { ToolContext, ToolDefinition, ToolResult } from "./types";

const imagineParamsSchema = z.object({
	prompt: z
		.string()
		.describe("A detailed description of the image to generate"),
});

type ImagineParams = z.infer<typeof imagineParamsSchema>;

async function executeImagine(
	params: ImagineParams,
	ctx: ToolContext,
): Promise<ToolResult> {
	const provider = new GoogleLLMProvider(
		getGoogleApiKeys(config),
		config.googleApiPaidKey,
	);

	try {
		const result = await provider.generateImage({
			model: "gemini-2.5-flash-preview-image",
			prompt: params.prompt,
			timeoutMs: 60_000,
		});

		const caption = captionPartsForMedia(params.prompt);
		await ctx.sendPhoto(result.image.data, caption.caption);
		for (const chunk of caption.followUpChunks) {
			await ctx.sendMessage(chunk);
		}
		return { handled: true };
	} catch (err) {
		const msg = err instanceof Error ? err.message : String(err);
		return { text: `Image generation failed: ${msg}`, error: msg };
	}
}

export const imagineTool: ToolDefinition<ImagineParams> = {
	name: "imagine",
	commands: ["/imagine", "/i", "/image", "/img"],
	description: "Generate an image from a text description",
	helpText: "tool-imagine",
	category: "media",
	parameters: imagineParamsSchema,
	execute: executeImagine,
	credits: 10,
	freeDaily: 1,
	capability: ModelCapability.IMAGE,
	defaultModel: "gemini-2.5-flash-preview-image",
};
