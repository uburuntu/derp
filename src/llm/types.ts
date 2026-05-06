/** LLM provider abstraction — Google-only at launch, but any provider can implement this interface */

import type { Database } from "../db/connection";

export interface LLMProvider {
	chat(params: ChatParams): Promise<ChatResult>;
	generateImage?(params: ImageParams): Promise<ImageResult>;
	generateVideo?(params: VideoParams): Promise<VideoResult>;
	synthesizeSpeech?(params: TTSParams): Promise<AudioResult>;
}

// ── Chat ─────────────────────────────────────────────────────────────────────

export interface ChatParams {
	model: string;
	systemPrompt: string;
	messages: ConversationMessage[];
	tools?: LLMToolSchema[];
	media?: MediaAttachment[];
	maxOutputTokens?: number;
	temperature?: number;
	timeoutMs?: number;
	tracking?: ProviderCallTracking;
}

export interface ChatResult {
	text: string;
	images?: BinaryMedia[];
	usage: TokenUsage;
	toolCalls?: ToolCallResult[];
	finishReason?: string;
	providerCallIds?: string[];
	actualModel?: string;
	costMicros?: number;
}

export interface ConversationMessage {
	role: "user" | "assistant";
	content: string;
	media?: MediaAttachment[];
}

export interface MediaAttachment {
	type: "image" | "video" | "audio" | "document";
	data: Buffer;
	mimeType: string;
	fileId?: string;
}

export interface TokenUsage {
	inputTokens: number;
	outputTokens: number;
	cacheHitTokens?: number;
}

// ── Image ────────────────────────────────────────────────────────────────────

export interface ImageParams {
	model: string;
	prompt: string;
	sourceImage?: Buffer;
	mimeType?: string;
	timeoutMs?: number;
	tracking?: ProviderCallTracking;
}

export interface ImageResult {
	image: BinaryMedia;
	usage?: TokenUsage;
	providerCallIds?: string[];
	costMicros?: number;
}

// ── Video ────────────────────────────────────────────────────────────────────

export interface VideoParams {
	model: string;
	prompt: string;
	referenceImage?: Buffer;
	timeoutMs?: number;
	tracking?: ProviderCallTracking;
}

export interface VideoResult {
	video: BinaryMedia;
	durationSeconds?: number;
	providerCallIds?: string[];
	costMicros?: number;
}

// ── TTS ──────────────────────────────────────────────────────────────────────

export interface TTSParams {
	model: string;
	text: string;
	voice?: string;
	timeoutMs?: number;
	tracking?: ProviderCallTracking;
}

export interface AudioResult {
	audio: Buffer;
	mimeType: string;
	durationSeconds?: number;
	providerCallIds?: string[];
	costMicros?: number;
}

// ── Shared ───────────────────────────────────────────────────────────────────

export interface BinaryMedia {
	data: Buffer;
	mimeType: string;
}

export interface LLMToolSchema {
	name: string;
	description: string;
	parameters: Record<string, unknown>; // JSON Schema
}

export interface ToolCallResult {
	name: string;
	args: Record<string, unknown>;
	result: unknown;
}

export interface ProviderCallTracking {
	db: Database;
	logicalRequestKey?: string;
	provider?: string;
	operation: string;
	route?: "primary" | "fallback";
	keyClass: "free" | "paid";
	userId?: string | null;
	chatId?: string | null;
	ledgerId?: string | null;
	messageId?: string | null;
	toolName?: string | null;
	creditsCharged?: number;
	creditSource?: string | null;
	mediaInputCount?: number;
	meta?: Record<string, unknown>;
}
