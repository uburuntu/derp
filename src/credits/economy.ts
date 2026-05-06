import { TOPUP_PACKS } from "./packs";
import { SUBSCRIPTION_PLANS } from "./subscriptions";

export const TELEGRAM_STAR_PAYOUT_USD = 0.013;
export const TARGET_GROSS_MARGIN = 0.3;

export function creditUsdFloor(): number {
    return Math.min(
        ...TOPUP_PACKS.map(
            (pack) => (pack.stars * TELEGRAM_STAR_PAYOUT_USD) / pack.credits,
        ),
        ...SUBSCRIPTION_PLANS.map(
            (plan) => (plan.stars * TELEGRAM_STAR_PAYOUT_USD) / plan.credits,
        ),
    );
}

export function providerCostToCredits(
    costUsd: number,
    margin = TARGET_GROSS_MARGIN,
): number {
    if (costUsd <= 0) return 1;
    return Math.max(1, Math.ceil(costUsd / (creditUsdFloor() * (1 - margin))));
}
