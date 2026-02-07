/** Top-up pack definitions — exact values from PRD SS7.5 */

export interface TopUpPack {
	id: string;
	label: string;
	stars: number;
	credits: number;
	bonus?: string;
}

export const TOPUP_PACKS: TopUpPack[] = [
	{ id: "micro", label: "Micro", stars: 50, credits: 50 },
	{ id: "small", label: "Small", stars: 150, credits: 150 },
	{ id: "medium", label: "Medium", stars: 500, credits: 550, bonus: "+10%" },
	{ id: "large", label: "Large", stars: 1500, credits: 1800, bonus: "+20%" },
];

export function getTopUpPack(id: string): TopUpPack | undefined {
	return TOPUP_PACKS.find((p) => p.id === id);
}
