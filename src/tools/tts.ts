/** Text-to-speech tool — converts text to voice messages */

import { z } from "zod";
import { convertToOggOpus } from "../common/ffmpeg";
import { config, getGoogleApiKeys } from "../config";
import { GoogleLLMProvider } from "../llm/providers/google";
import { ModelCapability } from "../llm/registry";
import type { ToolContext, ToolDefinition, ToolResult } from "./types";

const ttsParamsSchema = z.object({
	text: z.string().describe("The text to convert to speech"),
	voice: z
		.string()
		.optional()
		.describe("Voice name (e.g., Kore, Charon, Fenrir, Aoede, Puck)"),
});

type TTSParams = z.infer<typeof ttsParamsSchema>;

async function executeTTS(
	params: TTSParams,
	ctx: ToolContext,
): Promise<ToolResult> {
	const provider = new GoogleLLMProvider(
		getGoogleApiKeys(config),
		config.googleApiPaidKey,
	);

	try {
		const result = await provider.synthesizeSpeech({
			model: "gemini-2.5-pro-preview-tts",
			text: params.text,
			voice: params.voice,
			timeoutMs: 30_000,
		});

		// Convert WAV to OGG Opus for Telegram voice messages
		const oggBuffer = await convertToOggOpus(result.audio);
		await ctx.sendVoice(oggBuffer);
		return { handled: true };
	} catch (err) {
		const msg = err instanceof Error ? err.message : String(err);
		return { text: `TTS failed: ${msg}`, error: msg };
	}
}

export const ttsTool: ToolDefinition<TTSParams> = {
	name: "tts",
	commands: ["/tts"],
	description:
		"Convert text to a voice message. Use this when the user asks to hear something, read aloud, narrate a story, or wants audio output.",
	helpText: "tool-tts",
	category: "media",
	parameters: ttsParamsSchema,
	execute: executeTTS,
	credits: 5,
	freeDaily: 0,
	capability: ModelCapability.VOICE,
	defaultModel: "gemini-2.5-pro-preview-tts",
};
