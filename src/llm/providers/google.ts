/** Google GenAI SDK provider — implements LLMProvider for Gemini models */

import {
	type Content,
	createPartFromFunctionResponse,
	type FunctionDeclaration,
	type GenerateContentResponse,
	GoogleGenAI,
	HarmBlockThreshold,
	HarmCategory,
	type Part,
} from "@google/genai";
import { derpMetrics, logger, withSpan } from "../../common/observability";
import type {
	AudioResult,
	BinaryMedia,
	ChatParams,
	ChatResult,
	ConversationMessage,
	ImageParams,
	ImageResult,
	LLMProvider,
	LLMToolSchema,
	TokenUsage,
	ToolCallResult,
	TTSParams,
	VideoParams,
	VideoResult,
} from "../types";

const SAFETY_SETTINGS = [
	{
		category: HarmCategory.HARM_CATEGORY_HARASSMENT,
		threshold: HarmBlockThreshold.BLOCK_ONLY_HIGH,
	},
	{
		category: HarmCategory.HARM_CATEGORY_HATE_SPEECH,
		threshold: HarmBlockThreshold.BLOCK_ONLY_HIGH,
	},
	{
		category: HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
		threshold: HarmBlockThreshold.BLOCK_ONLY_HIGH,
	},
	{
		category: HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
		threshold: HarmBlockThreshold.BLOCK_ONLY_HIGH,
	},
];

const DEFAULT_CHAT_TIMEOUT = 30_000;
const DEFAULT_IMAGE_TIMEOUT = 60_000;
const DEFAULT_VIDEO_TIMEOUT = 180_000;
const MAX_TOOL_CALLS = 5;
const RETRY_DELAY_MS = 2_000;

/** Check if an error is transient and retryable */
function isTransientError(err: unknown): boolean {
	if (err instanceof Error) {
		const msg = err.message.toLowerCase();
		return (
			msg.includes("500") ||
			msg.includes("503") ||
			msg.includes("429") ||
			msg.includes("rate limit") ||
			msg.includes("overloaded") ||
			msg.includes("internal") ||
			msg.includes("unavailable") ||
			msg.includes("timeout") ||
			msg.includes("aborted")
		);
	}
	return false;
}

/** Sleep for a given duration */
function sleep(ms: number): Promise<void> {
	return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Round-robin key selector */
class KeyRotator {
	private index = 0;
	constructor(private keys: string[]) {
		if (keys.length === 0) throw new Error("No API keys provided");
	}
	next(): string {
		const key = this.keys[this.index % this.keys.length]!;
		this.index++;
		return key;
	}
}

/** Convert our ConversationMessage[] to Google Content[] */
function toGoogleContents(messages: ConversationMessage[]): Content[] {
	return messages.map((msg) => {
		const parts: Part[] = [];

		// Add media attachments
		if (msg.media) {
			for (const m of msg.media) {
				parts.push({
					inlineData: {
						data: m.data.toString("base64"),
						mimeType: m.mimeType,
					},
				});
			}
		}

		// Add text content
		if (msg.content) {
			parts.push({ text: msg.content });
		}

		return {
			role: msg.role === "assistant" ? "model" : "user",
			parts,
		};
	});
}

/** Convert our LLMToolSchema[] to Google FunctionDeclaration[] */
function toGoogleFunctionDeclarations(
	tools: LLMToolSchema[],
): FunctionDeclaration[] {
	return tools.map((tool) => ({
		name: tool.name,
		description: tool.description,
		parameters: tool.parameters as FunctionDeclaration["parameters"],
	}));
}

/** Extract token usage from response */
function extractUsage(response: GenerateContentResponse): TokenUsage {
	const meta = response.usageMetadata;
	return {
		inputTokens: meta?.promptTokenCount ?? 0,
		outputTokens: meta?.candidatesTokenCount ?? 0,
		cacheHitTokens: meta?.cachedContentTokenCount ?? 0,
	};
}

/** Extract text from response parts without using .text accessor (which warns on functionCall parts) */
function extractText(response: GenerateContentResponse): string {
	const parts = response.candidates?.[0]?.content?.parts;
	if (!parts) return "";
	return parts
		.filter((p) => p.text != null)
		.map((p) => p.text!)
		.join("");
}

export class GoogleLLMProvider implements LLMProvider {
	private keyRotator: KeyRotator;
	private paidKey: string | undefined;

	constructor(apiKeys: string[], paidKey?: string) {
		this.keyRotator = new KeyRotator(apiKeys);
		this.paidKey = paidKey;
	}

	private getClient(usePaidKey = false): GoogleGenAI {
		const key =
			usePaidKey && this.paidKey ? this.paidKey : this.keyRotator.next();
		return new GoogleGenAI({ apiKey: key });
	}

	async chat(params: ChatParams): Promise<ChatResult> {
		const ai = this.getClient();
		const timeoutMs = params.timeoutMs ?? DEFAULT_CHAT_TIMEOUT;

		const attempt = async (): Promise<ChatResult> => {
			const abortController = new AbortController();
			const timeout = setTimeout(() => abortController.abort(), timeoutMs);

			try {
				const contents = toGoogleContents(params.messages);

				// Add inline media from params.media to the last user message
				if (params.media && params.media.length > 0) {
					const lastUserIdx = contents.findLastIndex((c) => c.role === "user");
					if (lastUserIdx >= 0) {
						const target = contents[lastUserIdx]!;
						if (!target.parts) target.parts = [];
						for (const m of params.media) {
							target.parts.unshift({
								inlineData: {
									data: m.data.toString("base64"),
									mimeType: m.mimeType,
								},
							});
						}
					}
				}

				const toolDeclarations = params.tools
					? toGoogleFunctionDeclarations(params.tools)
					: undefined;

				const response = await ai.models.generateContent({
					model: params.model,
					contents,
					config: {
						systemInstruction: params.systemPrompt,
						safetySettings: SAFETY_SETTINGS,
						maxOutputTokens: params.maxOutputTokens,
						temperature: params.temperature,
						tools: toolDeclarations
							? [{ functionDeclarations: toolDeclarations }]
							: undefined,
						abortSignal: abortController.signal,
					},
				});

				const text = extractText(response);
				const usage = extractUsage(response);
				const finishReason = response.candidates?.[0]?.finishReason;

				this.recordLlmMetrics(params.model, usage);

				const images: BinaryMedia[] = [];
				for (const candidate of response.candidates ?? []) {
					for (const part of candidate.content?.parts ?? []) {
						if (part.inlineData?.data) {
							images.push({
								data: Buffer.from(part.inlineData.data, "base64"),
								mimeType: part.inlineData.mimeType ?? "image/png",
							});
						}
					}
				}

				const functionCalls = response.functionCalls;
				const toolCalls: ToolCallResult[] | undefined = functionCalls?.map(
					(fc) => ({
						name: fc.name ?? "",
						args: (fc.args as Record<string, unknown>) ?? {},
						result: undefined,
					}),
				);

				return {
					text,
					images: images.length > 0 ? images : undefined,
					usage,
					toolCalls: toolCalls && toolCalls.length > 0 ? toolCalls : undefined,
					finishReason: finishReason ?? undefined,
				};
			} finally {
				clearTimeout(timeout);
			}
		};

		// Try once, retry on transient error
		try {
			return await attempt();
		} catch (err) {
			if (isTransientError(err)) {
				await sleep(RETRY_DELAY_MS);
				return await attempt();
			}
			throw err;
		}
	}

	/**
	 * Run an agentic loop: call LLM, execute tool calls, feed results back.
	 * Continues until the LLM stops requesting tools or max iterations reached.
	 */
	async chatWithTools(
		params: ChatParams,
		executeTool: (
			name: string,
			args: Record<string, unknown>,
		) => Promise<unknown>,
	): Promise<ChatResult> {
		return withSpan(
			"gen_ai.chat_with_tools",
			{
				"gen_ai.system": "google",
				"gen_ai.request.model": params.model,
			},
			async (parentSpan) => {
				return this._chatWithToolsInner(params, executeTool, parentSpan);
			},
		);
	}

	private async _chatWithToolsInner(
		params: ChatParams,
		executeTool: (
			name: string,
			args: Record<string, unknown>,
		) => Promise<unknown>,
		parentSpan: import("@opentelemetry/api").Span,
	): Promise<ChatResult> {
		const ai = this.getClient();
		const timeoutMs = params.timeoutMs ?? DEFAULT_CHAT_TIMEOUT;
		const contents = toGoogleContents(params.messages);

		// Add inline media to the last user message
		if (params.media && params.media.length > 0) {
			const lastUserIdx = contents.findLastIndex((c) => c.role === "user");
			if (lastUserIdx >= 0) {
				const target = contents[lastUserIdx]!;
				if (!target.parts) target.parts = [];
				for (const m of params.media) {
					target.parts.unshift({
						inlineData: {
							data: m.data.toString("base64"),
							mimeType: m.mimeType,
						},
					});
				}
			}
		}

		const toolDeclarations = params.tools
			? toGoogleFunctionDeclarations(params.tools)
			: undefined;

		const totalUsage: TokenUsage = {
			inputTokens: 0,
			outputTokens: 0,
			cacheHitTokens: 0,
		};
		const allToolCalls: ToolCallResult[] = [];
		const allImages: BinaryMedia[] = [];
		let iteration = 0;

		while (iteration < MAX_TOOL_CALLS) {
			iteration++;
			const abortController = new AbortController();
			const timeout = setTimeout(() => abortController.abort(), timeoutMs);

			let response: GenerateContentResponse;
			try {
				response = await ai.models.generateContent({
					model: params.model,
					contents,
					config: {
						systemInstruction: params.systemPrompt,
						safetySettings: SAFETY_SETTINGS,
						maxOutputTokens: params.maxOutputTokens,
						temperature: params.temperature,
						tools: toolDeclarations
							? [{ functionDeclarations: toolDeclarations }]
							: undefined,
						abortSignal: abortController.signal,
					},
				});
			} finally {
				clearTimeout(timeout);
			}

			// Accumulate usage
			const usage = extractUsage(response);
			totalUsage.inputTokens += usage.inputTokens;
			totalUsage.outputTokens += usage.outputTokens;
			totalUsage.cacheHitTokens =
				(totalUsage.cacheHitTokens ?? 0) + (usage.cacheHitTokens ?? 0);

			// Extract images
			for (const candidate of response.candidates ?? []) {
				for (const part of candidate.content?.parts ?? []) {
					if (part.inlineData?.data) {
						allImages.push({
							data: Buffer.from(part.inlineData.data, "base64"),
							mimeType: part.inlineData.mimeType ?? "image/png",
						});
					}
				}
			}

			const functionCalls = response.functionCalls;
			if (!functionCalls || functionCalls.length === 0) {
				// No more tool calls — record metrics and return
				this.recordLlmMetrics(params.model, totalUsage);
				parentSpan.setAttribute(
					"gen_ai.usage.input_tokens",
					totalUsage.inputTokens,
				);
				parentSpan.setAttribute(
					"gen_ai.usage.output_tokens",
					totalUsage.outputTokens,
				);
				parentSpan.setAttribute("derp.tool_calls.count", allToolCalls.length);
				parentSpan.setAttribute("derp.iterations", iteration);
				return {
					text: extractText(response),
					images: allImages.length > 0 ? allImages : undefined,
					usage: totalUsage,
					toolCalls: allToolCalls.length > 0 ? allToolCalls : undefined,
					finishReason: response.candidates?.[0]?.finishReason ?? undefined,
				};
			}

			// Add the model's response (with function calls) to the conversation
			const modelParts: Part[] = [];
			const responseText = extractText(response);
			if (responseText) {
				modelParts.push({ text: responseText });
			}
			for (const fc of functionCalls) {
				modelParts.push({
					functionCall: {
						name: fc.name ?? "",
						args: (fc.args as Record<string, unknown>) ?? {},
					},
				});
			}
			contents.push({ role: "model", parts: modelParts });

			// Execute each function call and collect results
			const responseParts: Part[] = [];
			for (const fc of functionCalls) {
				const name = fc.name ?? "";
				const args = (fc.args as Record<string, unknown>) ?? {};

				let result: unknown;
				try {
					result = await executeTool(name, args);
				} catch (err) {
					result = {
						error: err instanceof Error ? err.message : String(err),
					};
				}

				allToolCalls.push({ name, args, result });
				responseParts.push(
					createPartFromFunctionResponse(
						fc.id ?? name,
						name,
						typeof result === "object" && result !== null
							? (result as Record<string, unknown>)
							: { result: String(result) },
					),
				);
			}

			// Add function responses to the conversation
			contents.push({ role: "user", parts: responseParts });
		}

		// Max iterations reached — record metrics and return
		this.recordLlmMetrics(params.model, totalUsage);
		parentSpan.setAttribute(
			"gen_ai.usage.input_tokens",
			totalUsage.inputTokens,
		);
		parentSpan.setAttribute(
			"gen_ai.usage.output_tokens",
			totalUsage.outputTokens,
		);
		parentSpan.setAttribute("derp.tool_calls.count", allToolCalls.length);
		parentSpan.setAttribute("derp.iterations", iteration);
		return {
			text: "[Maximum tool calls reached]",
			images: allImages.length > 0 ? allImages : undefined,
			usage: totalUsage,
			toolCalls: allToolCalls.length > 0 ? allToolCalls : undefined,
		};
	}

	async generateImage(params: ImageParams): Promise<ImageResult> {
		const ai = this.getClient(true); // Use paid key for image gen
		const timeoutMs = params.timeoutMs ?? DEFAULT_IMAGE_TIMEOUT;
		const abortController = new AbortController();
		const timeout = setTimeout(() => abortController.abort(), timeoutMs);

		try {
			const contents: Content[] = [];
			const parts: Part[] = [];

			// Add source image if editing
			if (params.sourceImage) {
				parts.push({
					inlineData: {
						data: params.sourceImage.toString("base64"),
						mimeType: params.mimeType ?? "image/jpeg",
					},
				});
			}

			parts.push({ text: params.prompt });
			contents.push({ role: "user", parts });

			const response = await ai.models.generateContent({
				model: params.model,
				contents,
				config: {
					responseModalities: ["TEXT", "IMAGE"],
					safetySettings: SAFETY_SETTINGS,
					abortSignal: abortController.signal,
				},
			});

			// Find image in response
			for (const candidate of response.candidates ?? []) {
				for (const part of candidate.content?.parts ?? []) {
					if (part.inlineData?.data) {
						return {
							image: {
								data: Buffer.from(part.inlineData.data, "base64"),
								mimeType: part.inlineData.mimeType ?? "image/png",
							},
							usage: extractUsage(response),
						};
					}
				}
			}

			throw new Error(
				"No image in response: " + (extractText(response) || "empty"),
			);
		} finally {
			clearTimeout(timeout);
		}
	}

	async generateVideo(params: VideoParams): Promise<VideoResult> {
		const ai = this.getClient(true); // Use paid key for video gen
		const timeoutMs = params.timeoutMs ?? DEFAULT_VIDEO_TIMEOUT;

		let operation = await ai.models.generateVideos({
			model: params.model,
			prompt: params.prompt,
			config: {
				numberOfVideos: 1,
				...(params.referenceImage
					? {
							image: {
								imageBytes: params.referenceImage.toString("base64"),
								mimeType: "image/jpeg",
							},
						}
					: {}),
			},
		});

		// Poll for completion
		const startTime = Date.now();
		while (!operation.done) {
			if (Date.now() - startTime > timeoutMs) {
				throw new Error("Video generation timed out");
			}
			await new Promise((resolve) => setTimeout(resolve, 5_000));
			operation = await ai.operations.getVideosOperation({
				operation,
			});
		}

		const generatedVideo = operation.response?.generatedVideos?.[0]?.video;
		if (!generatedVideo?.uri) {
			throw new Error("No video generated");
		}

		// Download the video from the URI
		const videoResponse = await fetch(generatedVideo.uri);
		if (!videoResponse.ok) {
			throw new Error(`Failed to download video: ${videoResponse.status}`);
		}
		const videoData = Buffer.from(await videoResponse.arrayBuffer());

		return {
			video: {
				data: videoData,
				mimeType: "video/mp4",
			},
			durationSeconds: 5,
		};
	}

	async synthesizeSpeech(params: TTSParams): Promise<AudioResult> {
		const ai = this.getClient();
		const timeoutMs = params.timeoutMs ?? DEFAULT_CHAT_TIMEOUT;
		const abortController = new AbortController();
		const timeout = setTimeout(() => abortController.abort(), timeoutMs);

		try {
			const response = await ai.models.generateContent({
				model: params.model,
				contents: params.text,
				config: {
					responseModalities: ["AUDIO"],
					speechConfig: {
						voiceConfig: {
							prebuiltVoiceConfig: {
								voiceName: params.voice ?? "Kore",
							},
						},
					},
					abortSignal: abortController.signal,
				},
			});

			// Extract audio from response
			for (const candidate of response.candidates ?? []) {
				for (const part of candidate.content?.parts ?? []) {
					if (part.inlineData?.data) {
						return {
							audio: Buffer.from(part.inlineData.data, "base64"),
							mimeType: part.inlineData.mimeType ?? "audio/wav",
						};
					}
				}
			}

			throw new Error("No audio in TTS response");
		} finally {
			clearTimeout(timeout);
		}
	}

	private recordLlmMetrics(model: string, usage: TokenUsage): void {
		derpMetrics.llmRequests.add(1, { model });
		derpMetrics.llmTokensInput.record(usage.inputTokens, { model });
		derpMetrics.llmTokensOutput.record(usage.outputTokens, { model });
	}
}
