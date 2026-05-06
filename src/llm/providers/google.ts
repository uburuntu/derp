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
	VideoGenerationReferenceType,
} from "@google/genai";
import { derpMetrics, withSpan } from "../../common/observability";
import {
	failProviderCall,
	finishProviderCall,
	startProviderCall,
} from "../../db/queries/finance";
import { estimateUsageCostMicros } from "../registry";
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
	ProviderCallTracking,
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
const DEFAULT_REQUEST_TIMEOUT = 30_000;
const MAX_TOOL_CALLS = 5;
const MAX_RETRY_ATTEMPTS = 2;
const RETRY_DELAY_MS = 2_000;
const MAX_GENERATED_VIDEO_BYTES = 50 * 1024 * 1024;
const BLOCKED_FINISH_REASONS = new Set([
	"SAFETY",
	"RECITATION",
	"LANGUAGE",
	"BLOCKLIST",
	"PROHIBITED_CONTENT",
	"SPII",
	"IMAGE_SAFETY",
	"IMAGE_PROHIBITED_CONTENT",
	"IMAGE_RECITATION",
]);

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

function remainingMs(deadlineMs: number): number {
	return Math.max(0, deadlineMs - Date.now());
}

function operationNameFrom(operation: unknown): string | null {
	if (!operation || typeof operation !== "object") return null;
	const maybeName = (operation as { name?: unknown }).name;
	return typeof maybeName === "string" && maybeName ? maybeName : null;
}

function attachPartialUsage(error: unknown, usage: TokenUsage): void {
	if (!(error instanceof Error)) return;
	if (usage.inputTokens <= 0 && usage.outputTokens <= 0) return;
	(
		error as Error & {
			partialUsage?: TokenUsage;
		}
	).partialUsage = { ...usage };
}

function partialUsageFrom(error: unknown): TokenUsage | null {
	if (!(error instanceof Error)) return null;
	const usage = (
		error as Error & {
			partialUsage?: TokenUsage;
		}
	).partialUsage;
	return usage ?? null;
}

class BillableProviderOutputError extends Error {
	billableFailure = true;
	providerCallIds?: string[];
	costMicros?: number;

	constructor(
		message: string,
		meta: { providerCallIds?: string[]; costMicros?: number } = {},
	) {
		super(message);
		this.name = "BillableProviderOutputError";
		this.providerCallIds = meta.providerCallIds;
		this.costMicros = meta.costMicros;
	}
}

async function withAbortTimeout<T>(
	timeoutMs: number,
	label: string,
	operation: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
	if (timeoutMs <= 0) {
		throw new Error(`${label} timed out`);
	}
	const abortController = new AbortController();
	const timeout = setTimeout(() => abortController.abort(), timeoutMs);

	try {
		return await operation(abortController.signal);
	} catch (err) {
		if (abortController.signal.aborted) {
			throw new Error(`${label} timed out after ${timeoutMs}ms`);
		}
		throw err;
	} finally {
		clearTimeout(timeout);
	}
}

async function readResponseLimited(
	response: Response,
	maxBytes: number,
	label: string,
): Promise<Buffer> {
	const contentLength = response.headers.get("content-length");
	if (contentLength) {
		const bytes = Number(contentLength);
		if (Number.isFinite(bytes) && bytes > maxBytes) {
			throw new Error(`${label} is too large: ${bytes} bytes`);
		}
	}

	if (!response.body) {
		const data = Buffer.from(await response.arrayBuffer());
		if (data.byteLength > maxBytes) {
			throw new Error(`${label} is too large: ${data.byteLength} bytes`);
		}
		return data;
	}

	const reader = response.body.getReader();
	const chunks: Buffer[] = [];
	let total = 0;

	while (true) {
		const { done, value } = await reader.read();
		if (done) break;
		if (!value) continue;

		total += value.byteLength;
		if (total > maxBytes) {
			await reader.cancel();
			throw new Error(`${label} is too large: ${total} bytes`);
		}
		chunks.push(Buffer.from(value));
	}

	return Buffer.concat(chunks, total);
}

/** Round-robin key selector */
class KeyRotator {
	private index = 0;
	constructor(private keys: string[]) {
		if (keys.length === 0) throw new Error("No API keys provided");
	}
	next(): string {
		const key = this.keys[this.index % this.keys.length];
		if (!key) throw new Error("No API keys configured");
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
		.map((p) => p.text ?? "")
		.filter(Boolean)
		.join("");
}

function fallbackTextForEmptyResponse(
	response: GenerateContentResponse,
): string {
	const blockReason = response.promptFeedback?.blockReason;
	if (blockReason) {
		return `I can't respond to that because the model blocked the request (${blockReason}).`;
	}

	const finishReason = response.candidates?.[0]?.finishReason;
	if (finishReason && BLOCKED_FINISH_REASONS.has(String(finishReason))) {
		return `I can't complete that because the model blocked the response (${finishReason}).`;
	}

	return "The model returned an empty response. Please try again.";
}

export class GoogleLLMProvider implements LLMProvider {
	private keyRotator: KeyRotator;
	private paidKey: string | undefined;

	constructor(apiKeys: string[], paidKey?: string) {
		this.keyRotator = new KeyRotator(apiKeys);
		this.paidKey = paidKey;
	}

	private getClient(usePaidKey = false): GoogleGenAI {
		if (usePaidKey && !this.paidKey) {
			throw new Error("Paid Google API key is not configured");
		}
		const key =
			usePaidKey && this.paidKey ? this.paidKey : this.keyRotator.next();
		return new GoogleGenAI({ apiKey: key });
	}

	private async startTracking(
		params: Pick<ChatParams, "model" | "tracking">,
		operation: string,
		mediaInputCount = 0,
	): Promise<string | null> {
		const tracking = params.tracking;
		if (!tracking) return null;
		return await startProviderCall(tracking.db, {
			logicalRequestKey: tracking.logicalRequestKey,
			provider: "google",
			operation,
			modelId: params.model,
			route: tracking.route,
			keyClass: tracking.keyClass,
			userId: tracking.userId,
			chatId: tracking.chatId,
			ledgerId: tracking.ledgerId,
			messageId: tracking.messageId,
			toolName: tracking.toolName,
			creditsCharged: tracking.creditsCharged,
			creditSource: tracking.creditSource,
			mediaInputCount,
			meta: tracking.meta,
		});
	}

	private async finishTracking(
		db: ProviderCallTracking["db"] | undefined,
		callId: string | null,
		model: string,
		usage: TokenUsage,
		input: {
			mediaInputCount?: number;
			mediaOutputCount?: number;
			durationSeconds?: number | null;
			finishReason?: string | null;
			includePerRequest?: boolean;
			providerRequestId?: string | null;
			meta?: Record<string, unknown>;
		} = {},
	): Promise<number> {
		if (!db || !callId) return estimateUsageCostMicros(model, usage, input);
		const costMicros = estimateUsageCostMicros(model, usage, input);
		await finishProviderCall(db, callId, {
			actualModelId: model,
			inputTokens: usage.inputTokens,
			outputTokens: usage.outputTokens,
			cacheHitTokens: usage.cacheHitTokens ?? 0,
			mediaInputCount: input.mediaInputCount,
			mediaOutputCount: input.mediaOutputCount,
			durationSeconds: input.durationSeconds,
			estimatedCostMicros: costMicros,
			actualCostMicros: costMicros,
			providerRequestId: input.providerRequestId,
			finishReason: input.finishReason,
			meta: input.meta,
		});
		return costMicros;
	}

	private async failTracking(
		db: ProviderCallTracking["db"] | undefined,
		callId: string | null,
		error: unknown,
		input: {
			providerRequestId?: string | null;
			meta?: Record<string, unknown>;
			estimatedCostMicros?: number;
			actualCostMicros?: number;
		} = {},
	): Promise<void> {
		if (!db || !callId) return;
		const message = error instanceof Error ? error.message : String(error);
		await failProviderCall(db, callId, {
			errorCode: isTransientError(error) ? "transient" : "provider_error",
			errorMessage: message,
			providerRequestId: input.providerRequestId,
			estimatedCostMicros: input.estimatedCostMicros,
			actualCostMicros: input.actualCostMicros,
			meta: input.meta,
		});
	}

	private async callWithRetries<T>(
		label: string,
		options: {
			usePaidKey?: boolean;
			deadlineMs: number;
			perAttemptTimeoutMs?: number;
		},
		operation: (ai: GoogleGenAI, signal: AbortSignal) => Promise<T>,
	): Promise<T> {
		let lastError: unknown;

		for (let attempt = 0; attempt < MAX_RETRY_ATTEMPTS; attempt++) {
			const timeLeft = remainingMs(options.deadlineMs);
			if (timeLeft <= 0) {
				throw new Error(`${label} timed out`);
			}

			const perAttemptTimeoutMs = Math.min(
				options.perAttemptTimeoutMs ?? timeLeft,
				timeLeft,
			);

			try {
				const ai = this.getClient(options.usePaidKey);
				return await withAbortTimeout(perAttemptTimeoutMs, label, (signal) =>
					operation(ai, signal),
				);
			} catch (err) {
				lastError = err;
				const canRetry =
					attempt < MAX_RETRY_ATTEMPTS - 1 &&
					isTransientError(err) &&
					remainingMs(options.deadlineMs) > RETRY_DELAY_MS;
				if (!canRetry) break;
				await sleep(Math.min(RETRY_DELAY_MS, remainingMs(options.deadlineMs)));
			}
		}

		throw lastError instanceof Error ? lastError : new Error(String(lastError));
	}

	async chat(params: ChatParams): Promise<ChatResult> {
		const timeoutMs = params.timeoutMs ?? DEFAULT_CHAT_TIMEOUT;
		const deadlineMs = Date.now() + timeoutMs;
		const callId = await this.startTracking(
			params,
			params.tracking?.operation ?? "chat",
			params.media?.length ?? 0,
		);

		const attempt = async (): Promise<ChatResult> => {
			const contents = toGoogleContents(params.messages);

			// Add inline media from params.media to the last user message
			if (params.media && params.media.length > 0) {
				const lastUserIdx = contents.findLastIndex((c) => c.role === "user");
				const target = lastUserIdx >= 0 ? contents[lastUserIdx] : undefined;
				if (target) {
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

			const response = await this.callWithRetries(
				"generateContent",
				{
					deadlineMs,
					usePaidKey: params.tracking?.keyClass === "paid",
				},
				(ai, signal) =>
					ai.models.generateContent({
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
							abortSignal: signal,
						},
					}),
			);

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
			const hasPayload =
				text ||
				images.length > 0 ||
				(toolCalls != null && toolCalls.length > 0);

			return {
				text: hasPayload ? text : fallbackTextForEmptyResponse(response),
				images: images.length > 0 ? images : undefined,
				usage,
				toolCalls: toolCalls && toolCalls.length > 0 ? toolCalls : undefined,
				finishReason: finishReason ?? undefined,
			};
		};
		try {
			const result = await attempt();
			const costMicros = await this.finishTracking(
				params.tracking?.db,
				callId,
				params.model,
				result.usage,
				{
					mediaInputCount: params.media?.length,
					mediaOutputCount: result.images?.length,
					finishReason: result.finishReason,
				},
			);
			return {
				...result,
				providerCallIds: callId ? [callId] : undefined,
				actualModel: params.model,
				costMicros,
			};
		} catch (err) {
			const partialUsage = partialUsageFrom(err);
			const partialCostMicros = partialUsage
				? estimateUsageCostMicros(params.model, partialUsage)
				: undefined;
			await this.failTracking(params.tracking?.db, callId, err, {
				estimatedCostMicros: partialCostMicros,
				actualCostMicros: partialCostMicros,
				meta: partialUsage ? { partialUsage } : undefined,
			});
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
		const callId = await this.startTracking(
			params,
			params.tracking?.operation ?? "chat_with_tools",
			params.media?.length ?? 0,
		);
		try {
			return await withSpan(
				"gen_ai.chat_with_tools",
				{
					"gen_ai.system": "google",
					"gen_ai.request.model": params.model,
				},
				async (parentSpan) => {
					const result = await this._chatWithToolsInner(
						params,
						executeTool,
						parentSpan,
					);
					const costMicros = await this.finishTracking(
						params.tracking?.db,
						callId,
						params.model,
						result.usage,
						{
							mediaInputCount: params.media?.length,
							mediaOutputCount: result.images?.length,
							finishReason: result.finishReason,
						},
					);
					return {
						...result,
						providerCallIds: callId ? [callId] : undefined,
						actualModel: params.model,
						costMicros,
					};
				},
			);
		} catch (err) {
			const partialUsage = partialUsageFrom(err);
			const partialCostMicros = partialUsage
				? estimateUsageCostMicros(params.model, partialUsage)
				: undefined;
			await this.failTracking(params.tracking?.db, callId, err, {
				estimatedCostMicros: partialCostMicros,
				actualCostMicros: partialCostMicros,
				meta: partialUsage ? { partialUsage } : undefined,
			});
			throw err;
		}
	}

	private async _chatWithToolsInner(
		params: ChatParams,
		executeTool: (
			name: string,
			args: Record<string, unknown>,
		) => Promise<unknown>,
		parentSpan: import("@opentelemetry/api").Span,
	): Promise<ChatResult> {
		const timeoutMs = params.timeoutMs ?? DEFAULT_CHAT_TIMEOUT;
		const deadlineMs = Date.now() + timeoutMs;
		const contents = toGoogleContents(params.messages);

		// Add inline media to the last user message
		if (params.media && params.media.length > 0) {
			const lastUserIdx = contents.findLastIndex((c) => c.role === "user");
			const target = lastUserIdx >= 0 ? contents[lastUserIdx] : undefined;
			if (target) {
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

		try {
			while (allToolCalls.length < MAX_TOOL_CALLS) {
				iteration++;

				const response = await this.callWithRetries(
					"generateContent",
					{
						deadlineMs,
						usePaidKey: params.tracking?.keyClass === "paid",
					},
					(ai, signal) =>
						ai.models.generateContent({
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
								abortSignal: signal,
							},
						}),
				);

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
					const text = extractText(response);
					const responseText = text
						? text
						: allImages.length > 0
							? ""
							: fallbackTextForEmptyResponse(response);
					return {
						text: responseText,
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
				const remainingToolCalls = MAX_TOOL_CALLS - allToolCalls.length;
				for (const fc of functionCalls.slice(0, remainingToolCalls)) {
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

				if (functionCalls.length > remainingToolCalls) {
					break;
				}

				// Add function responses to the conversation
				contents.push({ role: "user", parts: responseParts });
			}
		} catch (err) {
			attachPartialUsage(err, totalUsage);
			throw err;
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
		const timeoutMs = params.timeoutMs ?? DEFAULT_IMAGE_TIMEOUT;
		const deadlineMs = Date.now() + timeoutMs;
		const callId = await this.startTracking(
			params,
			params.tracking?.operation ?? "image",
			params.sourceImage ? 1 : 0,
		);
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

		let response: GenerateContentResponse;
		try {
			response = await this.callWithRetries(
				"generateImage",
				{ usePaidKey: true, deadlineMs },
				(ai, signal) =>
					ai.models.generateContent({
						model: params.model,
						contents,
						config: {
							responseModalities: ["TEXT", "IMAGE"],
							safetySettings: SAFETY_SETTINGS,
							abortSignal: signal,
						},
					}),
			);
		} catch (err) {
			await this.failTracking(params.tracking?.db, callId, err);
			throw err;
		}

		// Find image in response
		for (const candidate of response.candidates ?? []) {
			for (const part of candidate.content?.parts ?? []) {
				if (part.inlineData?.data) {
					const usage = extractUsage(response);
					const costMicros = await this.finishTracking(
						params.tracking?.db,
						callId,
						params.model,
						usage,
						{
							mediaInputCount: params.sourceImage ? 1 : 0,
							mediaOutputCount: 1,
						},
					);
					return {
						image: {
							data: Buffer.from(part.inlineData.data, "base64"),
							mimeType: part.inlineData.mimeType ?? "image/png",
						},
						usage,
						providerCallIds: callId ? [callId] : undefined,
						costMicros,
					};
				}
			}
		}

		const error = new Error(
			`No image in response: ${
				extractText(response) || fallbackTextForEmptyResponse(response)
			}`,
		);
		await this.failTracking(params.tracking?.db, callId, error);
		throw error;
	}

	async generateVideo(params: VideoParams): Promise<VideoResult> {
		const timeoutMs = params.timeoutMs ?? DEFAULT_VIDEO_TIMEOUT;
		const deadlineMs = Date.now() + timeoutMs;
		const callId = await this.startTracking(
			params,
			params.tracking?.operation ?? "video",
			params.referenceImage ? 1 : 0,
		);
		let operationName: string | null = null;
		let trackingFinished = false;

		try {
			let operation = await this.callWithRetries(
				"generateVideo",
				{
					usePaidKey: true,
					deadlineMs,
					perAttemptTimeoutMs: DEFAULT_REQUEST_TIMEOUT,
				},
				(ai, signal) =>
					ai.models.generateVideos({
						model: params.model,
						prompt: params.prompt,
						config: {
							numberOfVideos: 1,
							abortSignal: signal,
							...(params.referenceImage
								? {
										referenceImages: [
											{
												image: {
													imageBytes: params.referenceImage.toString("base64"),
													mimeType: "image/jpeg",
												},
												referenceType: VideoGenerationReferenceType.ASSET,
											},
										],
									}
								: {}),
						},
					}),
			);
			operationName = operationNameFrom(operation);

			// Poll for completion
			while (!operation.done) {
				if (remainingMs(deadlineMs) <= 0) {
					throw new Error("Video generation timed out");
				}
				await sleep(Math.min(5_000, remainingMs(deadlineMs)));
				operation = await this.callWithRetries(
					"getVideosOperation",
					{
						usePaidKey: true,
						deadlineMs,
						perAttemptTimeoutMs: DEFAULT_REQUEST_TIMEOUT,
					},
					(ai, signal) =>
						ai.operations.getVideosOperation({
							operation,
							config: { abortSignal: signal },
						}),
				);
				operationName = operationNameFrom(operation) ?? operationName;
			}

			if (operation.error) {
				throw new Error(
					`Video generation failed: ${JSON.stringify(operation.error)}`,
				);
			}
			const generatedVideo = operation.response?.generatedVideos?.[0]?.video;
			if (!generatedVideo?.uri) {
				const reasons = operation.response?.raiMediaFilteredReasons?.join(", ");
				throw new Error(
					reasons ? `No video generated: ${reasons}` : "No video generated",
				);
			}
			const usage = { inputTokens: 0, outputTokens: 0, cacheHitTokens: 0 };
			const costMicros = await this.finishTracking(
				params.tracking?.db,
				callId,
				params.model,
				usage,
				{
					mediaInputCount: params.referenceImage ? 1 : 0,
					mediaOutputCount: 1,
					durationSeconds: 5,
					providerRequestId: operationName,
					meta: operationName
						? { googleOperationName: operationName }
						: undefined,
				},
			);
			trackingFinished = true;

			// Download the video from the URI
			let videoData: Buffer;
			try {
				const videoResponse = await withAbortTimeout(
					Math.min(DEFAULT_REQUEST_TIMEOUT, remainingMs(deadlineMs)),
					"downloadGeneratedVideo",
					(signal) => fetch(generatedVideo.uri as string, { signal }),
				);
				if (!videoResponse.ok) {
					throw new Error(`Failed to download video: ${videoResponse.status}`);
				}
				videoData = await readResponseLimited(
					videoResponse,
					MAX_GENERATED_VIDEO_BYTES,
					"Generated video",
				);
			} catch (err) {
				throw new BillableProviderOutputError(
					`Video generated, but download failed: ${
						err instanceof Error ? err.message : String(err)
					}`,
					{
						providerCallIds: callId ? [callId] : undefined,
						costMicros,
					},
				);
			}

			return {
				video: {
					data: videoData,
					mimeType: "video/mp4",
				},
				durationSeconds: 5,
				providerCallIds: callId ? [callId] : undefined,
				costMicros,
			};
		} catch (err) {
			if (!trackingFinished) {
				await this.failTracking(params.tracking?.db, callId, err, {
					providerRequestId: operationName,
					meta: operationName
						? { googleOperationName: operationName }
						: undefined,
				});
			}
			throw err;
		}
	}

	async synthesizeSpeech(params: TTSParams): Promise<AudioResult> {
		const timeoutMs = params.timeoutMs ?? DEFAULT_CHAT_TIMEOUT;
		const deadlineMs = Date.now() + timeoutMs;
		const callId = await this.startTracking(
			params,
			params.tracking?.operation ?? "tts",
			0,
		);

		let response: GenerateContentResponse;
		try {
			response = await this.callWithRetries(
				"synthesizeSpeech",
				{
					deadlineMs,
					usePaidKey: params.tracking?.keyClass === "paid",
				},
				(ai, signal) =>
					ai.models.generateContent({
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
							abortSignal: signal,
						},
					}),
			);
		} catch (err) {
			await this.failTracking(params.tracking?.db, callId, err);
			throw err;
		}

		// Extract audio from response
		for (const candidate of response.candidates ?? []) {
			for (const part of candidate.content?.parts ?? []) {
				if (part.inlineData?.data) {
					const usage = extractUsage(response);
					const costMicros = await this.finishTracking(
						params.tracking?.db,
						callId,
						params.model,
						usage,
						{ mediaOutputCount: 1 },
					);
					return {
						audio: Buffer.from(part.inlineData.data, "base64"),
						mimeType: part.inlineData.mimeType ?? "audio/wav",
						providerCallIds: callId ? [callId] : undefined,
						costMicros,
					};
				}
			}
		}

		const error = new Error(
			`No audio in TTS response: ${fallbackTextForEmptyResponse(response)}`,
		);
		await this.failTracking(params.tracking?.db, callId, error);
		throw error;
	}

	private recordLlmMetrics(model: string, usage: TokenUsage): void {
		derpMetrics.llmRequests.add(1, { model });
		derpMetrics.llmTokensInput.record(usage.inputTokens, { model });
		derpMetrics.llmTokensOutput.record(usage.outputTokens, { model });
	}
}
