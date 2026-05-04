import type { UserPreferences } from "../db/schema";

export type ResponseStyle = "concise" | "balanced" | "detailed";

const RESPONSE_STYLES: ResponseStyle[] = ["concise", "balanced", "detailed"];
const DEFAULT_RESPONSE_STYLE: ResponseStyle = "balanced";

export function normalizeUserPreferences(
	preferences: UserPreferences | null | undefined,
): Required<UserPreferences> {
	const responseStyle = RESPONSE_STYLES.includes(
		preferences?.responseStyle as ResponseStyle,
	)
		? (preferences?.responseStyle as ResponseStyle)
		: DEFAULT_RESPONSE_STYLE;

	return {
		responseStyle,
		customInstructions: preferences?.customInstructions?.trim() || null,
		disabledTools: preferences?.disabledTools ?? [],
	};
}

export function nextResponseStyle(current: ResponseStyle): ResponseStyle {
	const index = RESPONSE_STYLES.indexOf(current);
	return RESPONSE_STYLES[(index + 1) % RESPONSE_STYLES.length] ?? "balanced";
}

export function formatUserPreferencesForPrompt(
	preferences: UserPreferences | null | undefined,
): string {
	const prefs = normalizeUserPreferences(preferences);
	const lines: string[] = [];

	if (prefs.responseStyle === "concise") {
		lines.push(
			"- Prefer concise answers. Lead with the answer, then one or two useful details.",
		);
	} else if (prefs.responseStyle === "detailed") {
		lines.push(
			"- Prefer detailed answers when the question benefits from explanation or tradeoffs.",
		);
	}

	if (prefs.customInstructions) {
		lines.push(`- User instructions: ${prefs.customInstructions}`);
	}

	if (prefs.disabledTools.length > 0) {
		lines.push(
			`- Do not suggest disabled tools: ${prefs.disabledTools.join(", ")}.`,
		);
	}

	return lines.join("\n");
}
