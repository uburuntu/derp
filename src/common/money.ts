/** Currency helpers shared by billing, providers, and reporting. */

export function usdToMicros(value: number): number {
    if (!Number.isFinite(value) || value <= 0) return 0;
    return Math.max(0, Math.round(value * 1_000_000));
}
