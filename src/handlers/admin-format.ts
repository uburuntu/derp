import { escapeHtml } from "../common/sanitize";
import type { RefundReconciliationResult } from "../db/queries/credits";

export function formatUsd(value: number): string {
    return `$${value.toFixed(2)}`;
}

export function formatReconciliation(
    chargeId: string,
    reconciliation: RefundReconciliationResult,
): string {
    const action = reconciliation.applied
        ? "Refund reconciled locally."
        : "Refund was already reconciled locally.";
    const unrecovered =
        reconciliation.unrecoveredAmount > 0
            ? `\nUnrecovered credits: ${reconciliation.unrecoveredAmount}`
            : "";

    return `${action}\nCharge: <code>${escapeHtml(chargeId)}</code>\nReversed: ${reconciliation.recoveredAmount}/${reconciliation.originalAmount} ${reconciliation.target} credits${unrecovered}\nBalance after: ${reconciliation.balanceAfter}`;
}
