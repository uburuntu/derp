"""Identifier-free immutable values exposed by the operator console."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Self


class OperatorMaintenanceAction(StrEnum):
    """Fixed maintenance passes available to trusted operators."""

    ALL = "all"
    HISTORY = "history"
    SUBSCRIPTIONS = "subscriptions"
    OPERATIONS = "operations"
    DELIVERIES = "deliveries"
    APPROVALS = "approvals"
    INFERENCE = "inference"


class OperatorDatabaseStatus(StrEnum):
    """Whether aggregate database diagnostics were available."""

    READY = "ready"
    DEGRADED = "degraded"


class OperatorProbeStatus(StrEnum):
    """State of one optional read-only provider diagnostic."""

    NOT_CONFIGURED = "not_configured"
    NOT_CHECKED = "not_checked"
    READY = "ready"
    FAILED = "failed"


class OperatorMaintenanceCompletion(StrEnum):
    """Conservative completion label for worker APIs that may swallow errors."""

    COMPLETED_PASS = "completed_pass"  # noqa: S105 - status vocabulary, not a secret


def _require_count(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _require_duration(value: float, name: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"{name} must be a finite non-negative number")


def _require_decimal(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError(f"{name} must be a finite non-negative Decimal")


def _require_finite_decimal(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")


@dataclass(frozen=True, slots=True)
class OperatorNamedCount:
    """One allowlisted aggregate bucket and its row count."""

    name: str
    count: int

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("count name must not be blank")
        if len(self.name) > 64:
            raise ValueError("count name is too long")
        _require_count(self.count, "count")


@dataclass(frozen=True, slots=True)
class OperatorActivityTotals:
    """Current total and rows recent by the aggregate's documented timestamp."""

    total: int
    recent_24h: int

    def __post_init__(self) -> None:
        _require_count(self.total, "total")
        _require_count(self.recent_24h, "recent_24h")
        if self.recent_24h > self.total:
            raise ValueError("recent_24h cannot exceed total")


@dataclass(frozen=True, slots=True)
class OperatorWalletTotals:
    """Canonical wallet inventory totals, excluding legacy credit columns."""

    available_credits: int
    reserved_credits: int
    consumed_credits: int
    debt_credits: int

    def __post_init__(self) -> None:
        for name in (
            "available_credits",
            "reserved_credits",
            "consumed_credits",
            "debt_credits",
        ):
            _require_count(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class OperatorStarsTotals:
    """Telegram Stars separated by current receipt settlement state."""

    fulfilled: int
    clawed_back: int

    def __post_init__(self) -> None:
        _require_count(self.fulfilled, "fulfilled")
        _require_count(self.clawed_back, "clawed_back")


@dataclass(frozen=True, slots=True)
class OperatorArtifactTotals:
    """Persisted artifact metadata totals without paths or content."""

    count: int
    bytes: int

    def __post_init__(self) -> None:
        _require_count(self.count, "count")
        _require_count(self.bytes, "bytes")


@dataclass(frozen=True, slots=True)
class OperatorSubscriptionTotals:
    """Subscription status, current entitlement, and renewal aggregates."""

    status_active: int
    entitled: int
    auto_renewing: int

    def __post_init__(self) -> None:
        _require_count(self.status_active, "status_active")
        _require_count(self.entitled, "entitled")
        _require_count(self.auto_renewing, "auto_renewing")
        if self.auto_renewing > self.entitled:
            raise ValueError("auto_renewing cannot exceed entitled")


@dataclass(frozen=True, slots=True)
class OperatorInferenceAttemptTotals:
    """Inference attempt totals partitioned by terminal state and time window."""

    total: int
    recent_24h: int
    succeeded: int
    succeeded_24h: int
    failed: int
    failed_24h: int
    pending: int
    pending_24h: int

    def __post_init__(self) -> None:
        for name in (
            "total",
            "recent_24h",
            "succeeded",
            "succeeded_24h",
            "failed",
            "failed_24h",
            "pending",
            "pending_24h",
        ):
            _require_count(getattr(self, name), name)
        if self.succeeded + self.failed + self.pending != self.total:
            raise ValueError("inference states must partition total attempts")
        if self.succeeded_24h + self.failed_24h + self.pending_24h != self.recent_24h:
            raise ValueError("recent inference states must partition recent attempts")
        if self.succeeded_24h > self.succeeded:
            raise ValueError("recent succeeded attempts cannot exceed their total")
        if self.failed_24h > self.failed:
            raise ValueError("recent failed attempts cannot exceed their total")
        if self.pending_24h > self.pending:
            raise ValueError("recent pending attempts cannot exceed their total")


@dataclass(frozen=True, slots=True)
class OperatorInferenceTokenTotals:
    """Provider-reported token usage without request or response content."""

    input: int
    output: int
    total: int
    cache_read: int
    cache_write: int
    reasoning: int
    audio_input: int
    audio_output: int

    def __post_init__(self) -> None:
        for name in (
            "input",
            "output",
            "total",
            "cache_read",
            "cache_write",
            "reasoning",
            "audio_input",
            "audio_output",
        ):
            _require_count(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class OperatorInferenceUsageTotals:
    """Aggregate inference accounting safe for the private operator console."""

    attempts: OperatorInferenceAttemptTotals
    tokens: OperatorInferenceTokenTotals
    pending_cost_reconciliation: int
    unavailable_cost_count: int
    reconciled_cost_usd: Decimal

    def __post_init__(self) -> None:
        _require_count(
            self.pending_cost_reconciliation,
            "pending_cost_reconciliation",
        )
        _require_count(self.unavailable_cost_count, "unavailable_cost_count")
        _require_decimal(self.reconciled_cost_usd, "reconciled_cost_usd")
        if self.pending_cost_reconciliation > self.attempts.total:
            raise ValueError("pending reconciliations cannot exceed inference attempts")
        if self.unavailable_cost_count > self.attempts.total:
            raise ValueError("unavailable costs cannot exceed inference attempts")


@dataclass(frozen=True, slots=True)
class OperatorInferenceCatalogSnapshot:
    """Reviewed local model-role coverage, independent of provider health."""

    verified_on: date
    enabled_roles: tuple[str, ...]
    available_roles: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.verified_on, date):
            raise TypeError("verified_on must be a date")
        if len(self.enabled_roles) != len(set(self.enabled_roles)):
            raise ValueError("enabled model roles must be unique")
        if len(self.available_roles) != len(set(self.available_roles)):
            raise ValueError("available model roles must be unique")
        if any(not role for role in (*self.enabled_roles, *self.available_roles)):
            raise ValueError("model roles must not be blank")
        if not set(self.available_roles).issubset(self.enabled_roles):
            raise ValueError("available model roles must be enabled")


@dataclass(frozen=True, slots=True)
class OperatorInferenceConnectivitySnapshot:
    """Cached outcomes from explicit read-only OpenRouter checks."""

    balance_status: OperatorProbeStatus
    catalog_status: OperatorProbeStatus
    key_limit_usd: Decimal | None = None
    key_usage_usd: Decimal | None = None
    key_remaining_usd: Decimal | None = None
    catalog_model_count: int | None = None
    visible_enabled_roles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        balance_values = (
            self.key_limit_usd,
            self.key_usage_usd,
            self.key_remaining_usd,
        )
        if self.balance_status is OperatorProbeStatus.READY:
            if self.key_usage_usd is None:
                raise ValueError("ready key checks require usage")
            _require_decimal(self.key_usage_usd, "key_usage_usd")
            if (self.key_limit_usd is None) != (self.key_remaining_usd is None):
                raise ValueError(
                    "key limit and remaining values must be present together"
                )
            if self.key_limit_usd is not None:
                _require_decimal(self.key_limit_usd, "key_limit_usd")
                _require_decimal(self.key_remaining_usd, "key_remaining_usd")
        elif any(value is not None for value in balance_values):
            raise ValueError("unavailable balance checks cannot carry balances")

        if self.catalog_status is OperatorProbeStatus.READY:
            if self.catalog_model_count is None:
                raise ValueError("ready catalog checks require a model count")
            _require_count(self.catalog_model_count, "catalog_model_count")
            if len(self.visible_enabled_roles) != len(set(self.visible_enabled_roles)):
                raise ValueError("visible enabled model roles must be unique")
        elif self.catalog_model_count is not None or self.visible_enabled_roles:
            raise ValueError("unavailable catalog checks cannot carry catalog data")

    @classmethod
    def not_configured(cls) -> Self:
        """Return the state used when no OpenRouter client is configured."""
        return cls(
            balance_status=OperatorProbeStatus.NOT_CONFIGURED,
            catalog_status=OperatorProbeStatus.NOT_CONFIGURED,
        )

    @classmethod
    def not_checked(cls) -> Self:
        """Return the initial state before an explicit provider check."""
        return cls(
            balance_status=OperatorProbeStatus.NOT_CHECKED,
            catalog_status=OperatorProbeStatus.NOT_CHECKED,
        )


@dataclass(frozen=True, slots=True)
class OperatorInferenceSnapshot:
    """Database accounting, reviewed catalog, and opt-in live diagnostics."""

    catalog: OperatorInferenceCatalogSnapshot
    connectivity: OperatorInferenceConnectivitySnapshot
    usage: OperatorInferenceUsageTotals | None = None


@dataclass(frozen=True, slots=True)
class OperatorPoolSnapshot:
    """Instantaneous non-sensitive database connection-pool gauges."""

    size: int
    checked_in: int
    checked_out: int
    overflow: int
    open_connections: int

    def __post_init__(self) -> None:
        for name in (
            "size",
            "checked_in",
            "checked_out",
            "overflow",
            "open_connections",
        ):
            _require_count(getattr(self, name), name)
        if self.open_connections != self.checked_in + self.checked_out:
            raise ValueError("open_connections must equal checked-in plus checked-out")


@dataclass(frozen=True, slots=True)
class OperatorDatabaseSnapshot:
    """Aggregate-only database diagnostics or an explicit degraded state."""

    status: OperatorDatabaseStatus
    latency_ms: float | None = None
    pool: OperatorPoolSnapshot | None = None
    users: OperatorActivityTotals | None = None
    chats: OperatorActivityTotals | None = None
    retained_messages: OperatorActivityTotals | None = None
    chats_by_type: tuple[OperatorNamedCount, ...] | None = None
    wallet: OperatorWalletTotals | None = None
    operation_states: tuple[OperatorNamedCount, ...] | None = None
    delivery_states: tuple[OperatorNamedCount, ...] | None = None
    approval_states: tuple[OperatorNamedCount, ...] | None = None
    intent_states: tuple[OperatorNamedCount, ...] | None = None
    receipt_states: tuple[OperatorNamedCount, ...] | None = None
    stars: OperatorStarsTotals | None = None
    subscriptions: OperatorSubscriptionTotals | None = None
    artifacts: OperatorArtifactTotals | None = None
    inference_usage: OperatorInferenceUsageTotals | None = None

    def __post_init__(self) -> None:
        aggregates = (
            self.latency_ms,
            self.pool,
            self.users,
            self.chats,
            self.retained_messages,
            self.chats_by_type,
            self.wallet,
            self.operation_states,
            self.delivery_states,
            self.approval_states,
            self.intent_states,
            self.receipt_states,
            self.stars,
            self.subscriptions,
            self.artifacts,
        )
        if self.status is OperatorDatabaseStatus.READY:
            if any(value is None for value in aggregates):
                raise ValueError("ready database snapshots require every aggregate")
            if self.latency_ms is None:
                raise ValueError("ready database snapshots require latency")
            _require_duration(self.latency_ms, "latency_ms")
        elif self.status is OperatorDatabaseStatus.DEGRADED:
            if any(value is not None for value in aggregates):
                raise ValueError("degraded database snapshots cannot carry aggregates")
        else:
            raise TypeError("status must be an OperatorDatabaseStatus")

    @classmethod
    def degraded(cls) -> Self:
        """Return an identifier-free unavailable database snapshot."""
        return cls(status=OperatorDatabaseStatus.DEGRADED)

    @property
    def is_degraded(self) -> bool:
        return self.status is OperatorDatabaseStatus.DEGRADED


@dataclass(frozen=True, slots=True)
class OperatorWorkerStatus:
    """Lifecycle running state for one runtime maintenance worker."""

    action: OperatorMaintenanceAction
    running: bool

    def __post_init__(self) -> None:
        if self.action is OperatorMaintenanceAction.ALL:
            raise ValueError("all is not a worker action")
        if not isinstance(self.running, bool):
            raise TypeError("running must be a bool")


@dataclass(frozen=True, slots=True)
class OperatorRuntimeSnapshot:
    """Process-local uptime and worker lifecycle state."""

    uptime_seconds: float
    workers: tuple[OperatorWorkerStatus, ...]

    def __post_init__(self) -> None:
        _require_duration(self.uptime_seconds, "uptime_seconds")
        expected = set(OperatorMaintenanceAction) - {OperatorMaintenanceAction.ALL}
        actions = {worker.action for worker in self.workers}
        if actions != expected or len(actions) != len(self.workers):
            raise ValueError("runtime snapshot requires each worker exactly once")


@dataclass(frozen=True, slots=True)
class OperatorConsoleSnapshot:
    """One aggregate-only view of process and database state."""

    runtime: OperatorRuntimeSnapshot
    database: OperatorDatabaseSnapshot
    inference: OperatorInferenceSnapshot | None = None


@dataclass(frozen=True, slots=True)
class OperatorMaintenancePass:
    """Observed counts from one worker pass, without claiming worker success."""

    action: OperatorMaintenanceAction
    counts: tuple[OperatorNamedCount, ...]
    completion: OperatorMaintenanceCompletion = (
        OperatorMaintenanceCompletion.COMPLETED_PASS
    )

    def __post_init__(self) -> None:
        if self.action is OperatorMaintenanceAction.ALL:
            raise ValueError("all is not an individual maintenance pass")
        if self.completion is not OperatorMaintenanceCompletion.COMPLETED_PASS:
            raise ValueError("maintenance pass completion is invalid")
        names = [count.name for count in self.counts]
        if len(names) != len(set(names)):
            raise ValueError("maintenance count names must be unique")


@dataclass(frozen=True, slots=True)
class OperatorMaintenanceResult:
    """Identifier-free result from one serialized operator request."""

    requested_action: OperatorMaintenanceAction
    passes: tuple[OperatorMaintenancePass, ...]
    duration_ms: float

    def __post_init__(self) -> None:
        _require_duration(self.duration_ms, "duration_ms")
        expected = (
            set(OperatorMaintenanceAction) - {OperatorMaintenanceAction.ALL}
            if self.requested_action is OperatorMaintenanceAction.ALL
            else {self.requested_action}
        )
        actions = {result.action for result in self.passes}
        if actions != expected or len(actions) != len(self.passes):
            raise ValueError("maintenance result passes do not match the request")


__all__ = [
    "OperatorActivityTotals",
    "OperatorArtifactTotals",
    "OperatorConsoleSnapshot",
    "OperatorDatabaseSnapshot",
    "OperatorDatabaseStatus",
    "OperatorInferenceAttemptTotals",
    "OperatorInferenceCatalogSnapshot",
    "OperatorInferenceConnectivitySnapshot",
    "OperatorInferenceSnapshot",
    "OperatorInferenceTokenTotals",
    "OperatorInferenceUsageTotals",
    "OperatorMaintenanceAction",
    "OperatorMaintenanceCompletion",
    "OperatorMaintenancePass",
    "OperatorMaintenanceResult",
    "OperatorNamedCount",
    "OperatorPoolSnapshot",
    "OperatorProbeStatus",
    "OperatorRuntimeSnapshot",
    "OperatorStarsTotals",
    "OperatorSubscriptionTotals",
    "OperatorWalletTotals",
    "OperatorWorkerStatus",
]
