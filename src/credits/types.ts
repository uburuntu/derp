import type { ModelTier } from "../llm/registry";

// ── Credit Check Result ──────────────────────────────────────────────────────

export interface CreditCheckResult {
	allowed: boolean;
	tier: ModelTier;
	modelId: string;
	source: "free" | "chat" | "user" | "rejected";
	creditsToDeduct: number;
	creditsRemaining: number | null;
	freeRemaining: number | null;
	rejectReason?: string;
}

// ── Transaction Types ────────────────────────────────────────────────────────

export type TransactionType =
	| "purchase"
	| "subscription"
	| "spend"
	| "refund"
	| "grant"
	| "transfer";
