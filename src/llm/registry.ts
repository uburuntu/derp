/** Model registry — tier-based model selection with pricing */

export enum ModelCapability {
	TEXT = "text",
	IMAGE = "image",
	VIDEO = "video",
	VOICE = "voice",
}

export enum ModelTier {
	FREE = "free",
	STANDARD = "standard",
	PREMIUM = "premium",
}

export interface ModelConfig {
	id: string;
	provider: string;
	displayName: string;
	capability: ModelCapability;
	tier: ModelTier;

	// Pricing (USD) — source of truth
	inputCostPer1M: number;
	outputCostPer1M: number;
	perRequestCost: number;

	// Capabilities
	maxContextTokens: number;
	supportsTools: boolean;
	supportsVision: boolean;

	// Registry
	isDefault: boolean;
	isDeprecated: boolean;

	// Computed
	creditCost: number;
}

// 1 credit ≈ $0.013 (1 Star developer payout)
const CREDIT_BASE_USD = 0.013;
const DEFAULT_MARGIN = 0.3;
const AVG_TOKENS_PER_REQUEST = 900; // ~650 in + ~250 out

function computeCreditCost(
	inputCostPer1M: number,
	outputCostPer1M: number,
	perRequestCost: number,
): number {
	const tokenCost =
		((inputCostPer1M + outputCostPer1M) * AVG_TOKENS_PER_REQUEST) / 1_000_000;
	const totalCost = tokenCost + perRequestCost;
	if (totalCost === 0) return 1;
	const withMargin = totalCost / (1 - DEFAULT_MARGIN);
	return Math.max(1, Math.ceil(withMargin / CREDIT_BASE_USD));
}

function defineModel(m: Omit<ModelConfig, "creditCost">): ModelConfig {
	return {
		...m,
		creditCost: computeCreditCost(
			m.inputCostPer1M,
			m.outputCostPer1M,
			m.perRequestCost,
		),
	};
}

// ── Model Definitions ────────────────────────────────────────────────────────

const MODELS: ModelConfig[] = [
	// Text — FREE tier
	defineModel({
		id: "gemini-2.5-flash-lite",
		provider: "google",
		displayName: "Gemini Flash Lite",
		capability: ModelCapability.TEXT,
		tier: ModelTier.FREE,
		inputCostPer1M: 0.1,
		outputCostPer1M: 0.4,
		perRequestCost: 0,
		maxContextTokens: 1_000_000,
		supportsTools: true,
		supportsVision: true,
		isDefault: true,
		isDeprecated: false,
	}),

	// Text — STANDARD tier
	defineModel({
		id: "gemini-2.5-flash",
		provider: "google",
		displayName: "Gemini Flash",
		capability: ModelCapability.TEXT,
		tier: ModelTier.STANDARD,
		inputCostPer1M: 0.4,
		outputCostPer1M: 2.5,
		perRequestCost: 0,
		maxContextTokens: 1_000_000,
		supportsTools: true,
		supportsVision: true,
		isDefault: true,
		isDeprecated: false,
	}),

	// Text — PREMIUM tier
	defineModel({
		id: "gemini-3-pro-preview",
		provider: "google",
		displayName: "Gemini 3 Pro",
		capability: ModelCapability.TEXT,
		tier: ModelTier.PREMIUM,
		inputCostPer1M: 2.0,
		outputCostPer1M: 12.0,
		perRequestCost: 0,
		maxContextTokens: 1_000_000,
		supportsTools: true,
		supportsVision: true,
		isDefault: true,
		isDeprecated: false,
	}),

	// Image — STANDARD tier
	defineModel({
		id: "gemini-2.5-flash-preview-image",
		provider: "google",
		displayName: "Gemini Flash Image",
		capability: ModelCapability.IMAGE,
		tier: ModelTier.STANDARD,
		inputCostPer1M: 0,
		outputCostPer1M: 0,
		perRequestCost: 0.039,
		maxContextTokens: 128_000,
		supportsTools: false,
		supportsVision: true,
		isDefault: true,
		isDeprecated: false,
	}),

	// Video — STANDARD tier (Veo 3.1 Fast, 5s default)
	defineModel({
		id: "veo-3.1-fast-generate-preview",
		provider: "google",
		displayName: "Veo 3.1 Fast",
		capability: ModelCapability.VIDEO,
		tier: ModelTier.STANDARD,
		inputCostPer1M: 0,
		outputCostPer1M: 0,
		perRequestCost: 0.75, // $0.15/sec * 5s
		maxContextTokens: 0,
		supportsTools: false,
		supportsVision: true,
		isDefault: true,
		isDeprecated: false,
	}),

	// Voice — STANDARD tier (Gemini TTS)
	defineModel({
		id: "gemini-2.5-pro-preview-tts",
		provider: "google",
		displayName: "Gemini TTS",
		capability: ModelCapability.VOICE,
		tier: ModelTier.STANDARD,
		inputCostPer1M: 1.0,
		outputCostPer1M: 20.0,
		perRequestCost: 0,
		maxContextTokens: 0,
		supportsTools: false,
		supportsVision: false,
		isDefault: true,
		isDeprecated: false,
	}),
];

// ── Registry ─────────────────────────────────────────────────────────────────

const MODEL_REGISTRY = new Map<string, ModelConfig>();
const DEFAULTS = new Map<string, ModelConfig>(); // key: `${capability}:${tier}`

for (const model of MODELS) {
	MODEL_REGISTRY.set(model.id, model);
	if (model.isDefault) {
		const key = `${model.capability}:${model.tier}`;
		if (DEFAULTS.has(key)) {
			throw new Error(
				`Duplicate default model for ${key}: ${DEFAULTS.get(key)!.id} and ${model.id}`,
			);
		}
		DEFAULTS.set(key, model);
	}
}

export function getModel(modelId: string): ModelConfig {
	const model = MODEL_REGISTRY.get(modelId);
	if (!model) throw new Error(`Unknown model: ${modelId}`);
	return model;
}

export function getDefaultModel(
	capability: ModelCapability,
	tier: ModelTier,
): ModelConfig {
	const key = `${capability}:${tier}`;
	const model = DEFAULTS.get(key);
	if (!model) throw new Error(`No default model for ${key}`);
	return model;
}

export function getAllModels(): ModelConfig[] {
	return [...MODEL_REGISTRY.values()];
}

// ── Context Limits ───────────────────────────────────────────────────────────

export const CONTEXT_LIMITS: Record<ModelTier, number> = {
	[ModelTier.FREE]: 15, // PRD §7.3: FREE gets 15 messages
	[ModelTier.STANDARD]: 100,
	[ModelTier.PREMIUM]: 100, // Registry completeness only — orchestrator never returns PREMIUM
};
