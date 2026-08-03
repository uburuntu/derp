"""Bounded, content-free OpenRouter actual-cost reconciliation."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import TracebackType
from typing import Final, Protocol, Self

import logfire

from derp.common.tasks import task_is_running
from derp.inference_usage import (
    MAX_RECONCILIATION_CLAIM_BATCH,
    CostReconciliationStatus,
    InferenceCostReconciliation,
    InferenceCostReconciliationClaim,
    InferenceReconciliationClaimLostError,
    InferenceTokenUsage,
    InferenceUsageRepository,
)
from derp.observability import report_exception
from derp.openrouter import (
    GenerationMetadata,
    OpenRouterError,
    OpenRouterHTTPError,
    OpenRouterTransportError,
)

OPENROUTER_PROVIDER = "openrouter"
DEFAULT_RECONCILIATION_LEASE = timedelta(minutes=15)
DEFAULT_RECONCILIATION_CONCURRENCY = 4
DEFAULT_STALE_INFERENCE_AGE = timedelta(hours=1)
MAX_RECONCILIATION_ATTEMPTS = 8
DEFAULT_OPENROUTER_RECONCILIATION_INTERVAL: Final = timedelta(minutes=5)
MAX_OPENROUTER_RECONCILIATION_INTERVAL: Final = timedelta(days=1)
MAX_OPENROUTER_RECONCILIATION_BATCH_SIZE: Final = MAX_RECONCILIATION_CLAIM_BATCH


class GenerationMetadataClient(Protocol):
    """Narrow OpenRouter surface needed by the reconciliation worker."""

    async def get_generation(self, generation_id: str) -> GenerationMetadata: ...


class ProtocolClock(Protocol):
    def __call__(self) -> datetime: ...


class ReconciliationDisposition(StrEnum):
    """Identifier-free result of one claimed metadata lookup."""

    RECONCILED = "reconciled"
    RETRY_SCHEDULED = "retry_scheduled"
    METADATA_UNAVAILABLE = "metadata_unavailable"
    CLAIM_LOST = "claim_lost"


@dataclass(frozen=True, slots=True)
class OpenRouterCostReconciliationReport:
    """Bounded aggregate outcome without provider or application identifiers."""

    claimed_count: int
    reconciled_count: int
    retry_scheduled_count: int
    unavailable_count: int
    claim_lost_count: int
    stale_attempt_count: int = 0

    def __post_init__(self) -> None:
        for name in (
            "claimed_count",
            "reconciled_count",
            "retry_scheduled_count",
            "unavailable_count",
            "claim_lost_count",
            "stale_attempt_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.claimed_count != (
            self.reconciled_count
            + self.retry_scheduled_count
            + self.unavailable_count
            + self.claim_lost_count
        ):
            raise ValueError("reconciliation outcome counts must equal claimed_count")

    @classmethod
    def from_dispositions(
        cls,
        dispositions: tuple[ReconciliationDisposition, ...],
        *,
        stale_attempt_count: int = 0,
    ) -> OpenRouterCostReconciliationReport:
        """Aggregate one bounded sweep without retaining record identities."""
        return cls(
            claimed_count=len(dispositions),
            reconciled_count=dispositions.count(ReconciliationDisposition.RECONCILED),
            retry_scheduled_count=dispositions.count(
                ReconciliationDisposition.RETRY_SCHEDULED
            ),
            unavailable_count=dispositions.count(
                ReconciliationDisposition.METADATA_UNAVAILABLE
            ),
            claim_lost_count=dispositions.count(ReconciliationDisposition.CLAIM_LOST),
            stale_attempt_count=stale_attempt_count,
        )

    @classmethod
    def empty(cls) -> OpenRouterCostReconciliationReport:
        """Return the identifier-free result of a sweep that examined nothing."""
        return cls(0, 0, 0, 0, 0)


class OpenRouterCostReconciliationRunner(Protocol):
    """Run one bounded reconciliation sweep."""

    async def reconcile(
        self,
        *,
        limit: int = 20,
    ) -> OpenRouterCostReconciliationReport: ...


class OpenRouterCostReconciliationService:
    """Lease pending records, fetch exact metadata, and reschedule uncertainty."""

    def __init__(
        self,
        repository: InferenceUsageRepository,
        client: GenerationMetadataClient,
        *,
        clock: ProtocolClock | None = None,
        lease_duration: timedelta = DEFAULT_RECONCILIATION_LEASE,
        concurrency: int = DEFAULT_RECONCILIATION_CONCURRENCY,
        stale_attempt_age: timedelta = DEFAULT_STALE_INFERENCE_AGE,
    ) -> None:
        if not isinstance(repository, InferenceUsageRepository):
            raise TypeError("repository must be an InferenceUsageRepository")
        if not callable(getattr(client, "get_generation", None)):
            raise TypeError("client must provide get_generation")
        if (
            not isinstance(lease_duration, timedelta)
            or lease_duration <= timedelta(0)
            or lease_duration > timedelta(hours=1)
        ):
            raise ValueError("lease_duration must be between 0 and 1 hour")
        if (
            isinstance(concurrency, bool)
            or not isinstance(concurrency, int)
            or not 1 <= concurrency <= 8
        ):
            raise ValueError("concurrency must be between 1 and 8")
        if (
            not isinstance(stale_attempt_age, timedelta)
            or stale_attempt_age <= timedelta(0)
            or stale_attempt_age > timedelta(days=1)
        ):
            raise ValueError("stale_attempt_age must be positive and at most one day")
        self._repository = repository
        self._client = client
        self._clock = clock or _utc_now
        if not callable(self._clock):
            raise TypeError("clock must be callable")
        self._lease_duration = lease_duration
        self._concurrency = concurrency
        self._stale_attempt_age = stale_attempt_age

    async def reconcile(
        self,
        *,
        limit: int = 20,
    ) -> OpenRouterCostReconciliationReport:
        """Run one bounded sweep; unexpected failures leave leases recoverable."""
        now = _aware_now(self._clock)
        stale_attempt_count = await self._repository.fail_stale_attempts(
            provider=OPENROUTER_PROVIDER,
            started_before=now - self._stale_attempt_age,
            failed_at=now,
            limit=limit,
        )
        claims = await self._repository.claim_pending_reconciliation(
            provider=OPENROUTER_PROVIDER,
            now=now,
            lease_duration=self._lease_duration,
            limit=limit,
        )
        semaphore = asyncio.Semaphore(self._concurrency)

        async def reconcile_claim(
            claim: InferenceCostReconciliationClaim,
        ) -> ReconciliationDisposition:
            async with semaphore:
                return await self._reconcile_claim(claim)

        dispositions = tuple(
            await asyncio.gather(*(reconcile_claim(claim) for claim in claims))
        )
        report = OpenRouterCostReconciliationReport.from_dispositions(
            dispositions,
            stale_attempt_count=stale_attempt_count,
        )
        logfire.info(
            "openrouter_cost_reconciliation_sweep",
            claimed_count=report.claimed_count,
            reconciled_count=report.reconciled_count,
            retry_scheduled_count=report.retry_scheduled_count,
            unavailable_count=report.unavailable_count,
            claim_lost_count=report.claim_lost_count,
            stale_attempt_count=report.stale_attempt_count,
        )
        return report

    async def _reconcile_claim(
        self,
        claim: InferenceCostReconciliationClaim,
    ) -> ReconciliationDisposition:
        generation_id = claim.provider_generation_id
        if generation_id is None:
            return await self._mark_unavailable(claim)

        try:
            with logfire.span(
                "openrouter.cost_reconciliation.fetch",
                reconciliation_attempt=claim.attempt,
            ):
                metadata = await self._client.get_generation(generation_id)
        except OpenRouterHTTPError as exc:
            if exc.retryable and claim.attempt < MAX_RECONCILIATION_ATTEMPTS:
                return await self._defer(
                    claim,
                    disposition=ReconciliationDisposition.RETRY_SCHEDULED,
                    delay=_retry_delay(claim.attempt, exc.retry_after),
                )
            return await self._mark_unavailable(claim)
        except OpenRouterTransportError:
            if claim.attempt >= MAX_RECONCILIATION_ATTEMPTS:
                return await self._mark_unavailable(claim)
            return await self._defer(
                claim,
                disposition=ReconciliationDisposition.RETRY_SCHEDULED,
                delay=_retry_delay(claim.attempt),
            )
        except OpenRouterError:
            return await self._mark_unavailable(claim)

        if metadata.id != generation_id or not _usage_matches(
            claim.expected_tokens,
            metadata,
        ):
            return await self._mark_unavailable(claim)

        reconciled_at = _aware_now(self._clock)
        try:
            reconciliation = InferenceCostReconciliation(
                actual_cost_usd=metadata.total_cost,
                reconciled_at=reconciled_at,
            )
        except TypeError, ValueError:
            return await self._mark_unavailable(claim)
        try:
            await self._repository.reconcile_claimed_cost(claim, reconciliation)
        except InferenceReconciliationClaimLostError:
            return ReconciliationDisposition.CLAIM_LOST
        return ReconciliationDisposition.RECONCILED

    async def _defer(
        self,
        claim: InferenceCostReconciliationClaim,
        *,
        disposition: ReconciliationDisposition,
        delay: timedelta,
    ) -> ReconciliationDisposition:
        deferred_at = _aware_now(self._clock)
        try:
            snapshot = await self._repository.defer_cost_reconciliation(
                claim,
                deferred_at=deferred_at,
                retry_at=deferred_at + delay,
            )
        except InferenceReconciliationClaimLostError:
            return ReconciliationDisposition.CLAIM_LOST
        if snapshot.reconciliation_status is CostReconciliationStatus.RECONCILED:
            return ReconciliationDisposition.CLAIM_LOST
        return disposition

    async def _mark_unavailable(
        self,
        claim: InferenceCostReconciliationClaim,
    ) -> ReconciliationDisposition:
        try:
            snapshot = await self._repository.mark_claimed_cost_unavailable(
                claim,
                unavailable_at=_aware_now(self._clock),
            )
        except InferenceReconciliationClaimLostError:
            return ReconciliationDisposition.CLAIM_LOST
        if snapshot.reconciliation_status is CostReconciliationStatus.RECONCILED:
            return ReconciliationDisposition.CLAIM_LOST
        return ReconciliationDisposition.METADATA_UNAVAILABLE


class OpenRouterCostReconciliationWorker:
    """Run startup and periodic bounded OpenRouter cost reconciliation."""

    def __init__(
        self,
        reconciler: OpenRouterCostReconciliationRunner,
        *,
        interval: timedelta = DEFAULT_OPENROUTER_RECONCILIATION_INTERVAL,
        batch_size: int = 20,
    ) -> None:
        if (
            not isinstance(interval, timedelta)
            or interval <= timedelta(0)
            or interval > MAX_OPENROUTER_RECONCILIATION_INTERVAL
        ):
            raise ValueError("interval must be positive and at most one day")
        if (
            isinstance(batch_size, bool)
            or not isinstance(batch_size, int)
            or not 1 <= batch_size <= MAX_OPENROUTER_RECONCILIATION_BATCH_SIZE
        ):
            raise ValueError(
                "batch_size must be between 1 and "
                f"{MAX_OPENROUTER_RECONCILIATION_BATCH_SIZE}"
            )
        if not callable(getattr(reconciler, "reconcile", None)):
            raise TypeError("reconciler must provide reconcile")
        self._reconciler = reconciler
        self._interval_seconds = interval.total_seconds()
        self._batch_size = batch_size
        self._sweep_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        """Whether the periodic task is alive rather than completed or failed."""
        return task_is_running(self._task)

    async def __aenter__(self) -> Self:
        if self._task is not None:
            raise RuntimeError(
                "OpenRouter cost reconciliation worker is already running"
            )
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run_periodically(),
            name="openrouter-cost-reconciliation",
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def sweep(self) -> OpenRouterCostReconciliationReport:
        """Run one serialized batch and expose only aggregate counts."""
        async with self._sweep_lock:
            try:
                return await self._reconciler.reconcile(limit=self._batch_size)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                report_exception(
                    "openrouter_cost_reconciliation_sweep_failed",
                    exception=exc,
                    level="warning",
                )
                return OpenRouterCostReconciliationReport.empty()

    async def aclose(self) -> None:
        """Signal and join the worker before its client and database close."""
        if (task := self._task) is None:
            return
        self._task = None
        self._stop.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run_periodically(self) -> None:
        await self.sweep()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._interval_seconds,
                )
            except TimeoutError:
                await self.sweep()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _aware_now(clock: ProtocolClock) -> datetime:
    value = clock()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _usage_matches(
    expected: InferenceTokenUsage | None,
    metadata: GenerationMetadata,
) -> bool:
    if expected is None:
        return True
    return (
        metadata.tokens_prompt is not None
        and metadata.tokens_completion is not None
        and metadata.tokens_prompt == expected.input_tokens
        and metadata.tokens_completion == expected.output_tokens
    )


def _retry_delay(attempt: int, retry_after: str | None = None) -> timedelta:
    seconds = min(30 * 2 ** min(attempt - 1, 5), 15 * 60)
    if (
        retry_after
        and len(retry_after) <= 10
        and retry_after.isascii()
        and retry_after.isdecimal()
    ):
        seconds = max(seconds, min(int(retry_after), 60 * 60))
    return timedelta(seconds=seconds)


__all__ = [
    "DEFAULT_STALE_INFERENCE_AGE",
    "MAX_RECONCILIATION_ATTEMPTS",
    "DEFAULT_RECONCILIATION_CONCURRENCY",
    "DEFAULT_RECONCILIATION_LEASE",
    "DEFAULT_OPENROUTER_RECONCILIATION_INTERVAL",
    "MAX_OPENROUTER_RECONCILIATION_BATCH_SIZE",
    "MAX_OPENROUTER_RECONCILIATION_INTERVAL",
    "OPENROUTER_PROVIDER",
    "OpenRouterCostReconciliationReport",
    "OpenRouterCostReconciliationRunner",
    "OpenRouterCostReconciliationService",
    "OpenRouterCostReconciliationWorker",
    "ReconciliationDisposition",
]
