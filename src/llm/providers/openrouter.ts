/** OpenRouter text provider — paid fallback only for metered chat. */

import {
	failProviderCall,
	finishProviderCall,
	startProviderCall,
	usdToMicros,
} from "../../db/queries/finance";
import type {
	ChatParams,
	ChatResult,
	ConversationMessage,
	LLMProvider,
	TokenUsage,
} from "../types";

const OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions";

interface OpenRouterUsage {
	prompt_tokens?: number;
	completion_tokens?: number;
	total_tokens?: number;
	prompt_tokens_details?: { cached_tokens?: number };
	cost?: number;
}

interface OpenRouterResponse {
	id?: string;
	model?: string;
	choices?: Array<{
		finish_reason?: string;
		message?: { content?: string };
	}>;
	usage?: OpenRouterUsage;
	error?: { code?: string; message?: string };
}

function withTimeout(timeoutMs: number): {
	signal: AbortSignal;
	cancel: () => void;
} {
	const controller = new AbortController();
	const timeout = setTimeout(() => controller.abort(), timeoutMs);
	return {
		signal: controller.signal,
		cancel: () => clearTimeout(timeout),
	};
}

function toOpenRouterMessages(
	systemPrompt: string,
	messages: ConversationMessage[],
): Array<{ role: "system" | "user" | "assistant"; content: string }> {
	return [
		{ role: "system", content: systemPrompt },
		...messages.map((message) => ({
			role: message.role,
			content: message.content,
		})),
	];
}

function usageFromOpenRouter(usage: OpenRouterUsage | undefined): TokenUsage {
	return {
		inputTokens: usage?.prompt_tokens ?? 0,
		outputTokens: usage?.completion_tokens ?? 0,
		cacheHitTokens: usage?.prompt_tokens_details?.cached_tokens ?? 0,
	};
}

export class OpenRouterProvider implements LLMProvider {
	constructor(
		private apiKey: string,
		private appName = "Derp",
	) {}

	async chat(params: ChatParams): Promise<ChatResult> {
		if (params.tracking?.keyClass !== "paid") {
			throw new Error("OpenRouter fallback is only enabled for paid routes");
		}
		if (params.tools?.length || params.media?.length) {
			throw new Error("OpenRouter fallback only supports text chat");
		}

		const callId = params.tracking
			? await startProviderCall(params.tracking.db, {
					logicalRequestKey: `${params.tracking.logicalRequestKey}:openrouter`,
					provider: "openrouter",
					operation: params.tracking.operation,
					modelId: params.model,
					route: "fallback",
					keyClass: "paid",
					userId: params.tracking.userId,
					chatId: params.tracking.chatId,
					ledgerId: params.tracking.ledgerId,
					toolName: params.tracking.toolName,
					creditsCharged: params.tracking.creditsCharged,
					creditSource: params.tracking.creditSource,
					meta: params.tracking.meta,
				})
			: null;

		const timeout = withTimeout(params.timeoutMs ?? 15_000);
		try {
			const response = await fetch(OPENROUTER_URL, {
				method: "POST",
				signal: timeout.signal,
				headers: {
					Authorization: `Bearer ${this.apiKey}`,
					"Content-Type": "application/json",
					"HTTP-Referer": "https://t.me/DerpRobot",
					"X-Title": this.appName,
				},
				body: JSON.stringify({
					model: params.model,
					messages: toOpenRouterMessages(params.systemPrompt, params.messages),
					max_tokens: params.maxOutputTokens,
					temperature: params.temperature,
					provider: {
						allow_fallbacks: true,
					},
				}),
			});

			const body = (await response.json()) as OpenRouterResponse;
			if (!response.ok || body.error) {
				throw new Error(
					body.error?.message ?? `OpenRouter failed: ${response.status}`,
				);
			}

			const usage = usageFromOpenRouter(body.usage);
			const costMicros = usdToMicros(body.usage?.cost ?? 0);
			if (params.tracking?.db && callId) {
				await finishProviderCall(params.tracking.db, callId, {
					actualModelId: body.model ?? params.model,
					inputTokens: usage.inputTokens,
					outputTokens: usage.outputTokens,
					cacheHitTokens: usage.cacheHitTokens ?? 0,
					actualCostMicros: costMicros,
					estimatedCostMicros: costMicros,
					providerRequestId: body.id,
					finishReason: body.choices?.[0]?.finish_reason,
				});
			}

			return {
				text: body.choices?.[0]?.message?.content ?? "",
				usage,
				finishReason: body.choices?.[0]?.finish_reason,
				providerCallIds: callId ? [callId] : undefined,
				actualModel: body.model,
				costMicros,
			};
		} catch (err) {
			if (params.tracking?.db && callId) {
				await failProviderCall(params.tracking.db, callId, {
					errorCode: "openrouter_error",
					errorMessage: err instanceof Error ? err.message : String(err),
				});
			}
			throw err;
		} finally {
			timeout.cancel();
		}
	}
}
