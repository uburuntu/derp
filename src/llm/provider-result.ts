/** Helpers for accumulating provider-call IDs and provider cost metadata. */

export interface ProviderResultMetadata {
    providerCallIds?: string[];
    costMicros?: number;
}

export function mergeProviderResultMetadata(
    target: ProviderResultMetadata,
    result: ProviderResultMetadata,
): void {
    if (result.providerCallIds?.length) {
        const ids = new Set([
            ...(target.providerCallIds ?? []),
            ...result.providerCallIds,
        ]);
        target.providerCallIds = [...ids];
    }
    if (result.costMicros && result.costMicros > 0) {
        target.costMicros = (target.costMicros ?? 0) + result.costMicros;
    }
}

export function hasProviderResultMetadata(
    meta: ProviderResultMetadata,
): boolean {
    return Boolean(meta.providerCallIds?.length || (meta.costMicros ?? 0) > 0);
}
