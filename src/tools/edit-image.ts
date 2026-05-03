/** Image editing tool — edits images based on text instructions */

import { z } from "zod";
import { config, getGoogleApiKeys } from "../config";
import { GoogleLLMProvider } from "../llm/providers/google";
import { ModelCapability } from "../llm/registry";
import type { ToolContext, ToolDefinition, ToolResult } from "./types";

const editImageParamsSchema = z.object({
	prompt: z.string().describe("Instructions for how to edit the image"),
});

type EditImageParams = z.infer<typeof editImageParamsSchema>;

async function executeEditImage(
	params: EditImageParams,
	ctx: ToolContext,
): Promise<ToolResult> {
	// Source image from the triggering message's media
	const sourceImage = ctx.replyMedia?.find((media) =>
		media.mimeType.startsWith("image/"),
	);
	if (!sourceImage) {
		return {
			text: "No image found to edit. Reply to a message with an image, or send an image with your edit instructions.",
			error: "No source image",
		};
	}

	const provider = new GoogleLLMProvider(
		getGoogleApiKeys(config),
		config.googleApiPaidKey,
	);

	try {
		const result = await provider.generateImage({
			model: "gemini-2.5-flash-preview-image",
			prompt: params.prompt,
			sourceImage: sourceImage.data,
			mimeType: sourceImage.mimeType,
			timeoutMs: 60_000,
		});

		await ctx.sendPhoto(result.image.data, params.prompt.slice(0, 1024));
		return { handled: true };
	} catch (err) {
		const msg = err instanceof Error ? err.message : String(err);
		return { text: `Image editing failed: ${msg}`, error: msg };
	}
}

export const editImageTool: ToolDefinition<EditImageParams> = {
	name: "editImage",
	commands: ["/edit", "/e", "/ed"],
	description: "Edit an image based on text instructions (reply to an image)",
	helpText: "tool-edit-image",
	category: "media",
	parameters: editImageParamsSchema,
	execute: executeEditImage,
	credits: 10,
	freeDaily: 1,
	capability: ModelCapability.IMAGE,
	defaultModel: "gemini-2.5-flash-preview-image",
};
