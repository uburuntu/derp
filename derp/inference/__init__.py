"""Privacy and admission policy for provider inference."""

from typing import TYPE_CHECKING, Any

from derp.inference.privacy import (
    FREE_INFERENCE_LEGAL_VERSION_MAX_LENGTH,
    FREE_INFERENCE_PRIVACY_URL,
    FREE_INFERENCE_PRIVACY_VERSION,
    FREE_INFERENCE_TOS_URL,
    FREE_INFERENCE_TOS_VERSION,
    InferenceContext,
    InferencePrivacyFields,
    InferencePrivacyMode,
    InferencePrivacyPreference,
    NonZdrFreeInferenceDecision,
    NonZdrFreeInferenceReason,
    decide_non_zdr_free_inference,
    project_inference_privacy,
)
from derp.inference.recorder import (
    InferenceAttempt,
    InferenceRecorder,
    InferenceRoutePolicyError,
)
from derp.inference.report import (
    InferenceReport,
    aggregate_reports,
    report_from_response,
    reports_from_messages,
)

if TYPE_CHECKING:
    from derp.inference.reconciliation import (
        DEFAULT_STALE_INFERENCE_AGE,
        MAX_RECONCILIATION_ATTEMPTS,
        OpenRouterCostReconciliationReport,
        OpenRouterCostReconciliationRunner,
        OpenRouterCostReconciliationService,
        OpenRouterCostReconciliationWorker,
        ReconciliationDisposition,
    )

_RECONCILIATION_EXPORTS = frozenset(
    {
        "DEFAULT_OPENROUTER_RECONCILIATION_INTERVAL",
        "DEFAULT_RECONCILIATION_CONCURRENCY",
        "DEFAULT_RECONCILIATION_LEASE",
        "DEFAULT_STALE_INFERENCE_AGE",
        "MAX_RECONCILIATION_ATTEMPTS",
        "MAX_OPENROUTER_RECONCILIATION_BATCH_SIZE",
        "MAX_OPENROUTER_RECONCILIATION_INTERVAL",
        "OPENROUTER_PROVIDER",
        "OpenRouterCostReconciliationReport",
        "OpenRouterCostReconciliationRunner",
        "OpenRouterCostReconciliationService",
        "OpenRouterCostReconciliationWorker",
        "ReconciliationDisposition",
    }
)

__all__ = [
    "FREE_INFERENCE_LEGAL_VERSION_MAX_LENGTH",
    "FREE_INFERENCE_PRIVACY_URL",
    "FREE_INFERENCE_PRIVACY_VERSION",
    "FREE_INFERENCE_TOS_URL",
    "FREE_INFERENCE_TOS_VERSION",
    "InferenceContext",
    "InferencePrivacyFields",
    "InferencePrivacyMode",
    "InferencePrivacyPreference",
    "NonZdrFreeInferenceDecision",
    "NonZdrFreeInferenceReason",
    "decide_non_zdr_free_inference",
    "project_inference_privacy",
    "InferenceAttempt",
    "InferenceRecorder",
    "InferenceRoutePolicyError",
    "DEFAULT_RECONCILIATION_CONCURRENCY",
    "DEFAULT_RECONCILIATION_LEASE",
    "DEFAULT_STALE_INFERENCE_AGE",
    "MAX_RECONCILIATION_ATTEMPTS",
    "DEFAULT_OPENROUTER_RECONCILIATION_INTERVAL",
    "MAX_OPENROUTER_RECONCILIATION_BATCH_SIZE",
    "MAX_OPENROUTER_RECONCILIATION_INTERVAL",
    "OPENROUTER_PROVIDER",
    "OpenRouterCostReconciliationReport",
    "OpenRouterCostReconciliationRunner",
    "OpenRouterCostReconciliationService",
    "OpenRouterCostReconciliationWorker",
    "ReconciliationDisposition",
    "InferenceReport",
    "aggregate_reports",
    "report_from_response",
    "reports_from_messages",
]


def __getattr__(name: str) -> Any:
    """Load reconciliation and its direct OpenRouter adapter only on demand."""
    if name not in _RECONCILIATION_EXPORTS:
        raise AttributeError(name)
    from derp.inference import reconciliation

    return getattr(reconciliation, name)
