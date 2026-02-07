/** LLM provider abstraction — Google-only at launch, but any provider can implement this interface */

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
}

export interface ChatResult {
	text: string;
	images?: BinaryMedia[];
	usage: TokenUsage;
	toolCalls?: ToolCallResult[];
	finishReason?: string;
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
}

export interface ImageResult {
	image: BinaryMedia;
	usage?: TokenUsage;
}

// ── Video ────────────────────────────────────────────────────────────────────

export interface VideoParams {
	model: string;
	prompt: string;
	referenceImage?: Buffer;
	timeoutMs?: number;
}

export interface VideoResult {
	video: BinaryMedia;
	durationSeconds?: number;
}

// ── TTS ──────────────────────────────────────────────────────────────────────

export interface TTSParams {
	model: string;
	text: string;
	voice?: string;
	timeoutMs?: number;
}

export interface AudioResult {
	audio: Buffer;
	mimeType: string;
	durationSeconds?: number;
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
