"""Durable approval workflow shared by paid command-media features."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Protocol

from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, Message
from pydantic import Field
from pydantic_ai import ModelMessage, ModelResponse, ToolCallPart

from derp.approvals.serialization import durable_message_history
from derp.approvals.service import (
    ApprovalDecisionConflictError,
    DeferredToolApprovalService,
)
from derp.approvals.types import (
    ApprovalCapability,
    ApprovalChoice,
    ApprovalDecision,
    DecisionDisposition,
    DeferredToolHandle,
    DeferredToolSnapshot,
    ResumeClaim,
    ResumeLease,
)
from derp.delivery import DeliveryTarget, ProgressStage
from derp.execution import ExecutionPlan, Feature, Outcome, Succeeded
from derp.features.paid_media_operation import (
    PaidMediaInvocation,
    PaidMediaNotCharged,
    PaidMediaNotChargedReason,
    PaidMediaOperationCoordinator,
    PaidMediaOperationOutcome,
    PaidMediaQuoteInput,
    PaidMediaResult,
)
from derp.observability import report_exception
from derp.operations import (
    InvalidOperationTransitionError,
    OperationId,
    OperationLedger,
    OperationRequestBinder,
    Quote,
)

type ProgressReporter = Callable[[ProgressStage], Awaitable[None]]


class PaidMediaApprovalError(RuntimeError):
    """A persisted paid-media approval violated its trusted contract."""


class PaidMediaApprovalKind(StrEnum):
    """Compact feature discriminators used only to route callback adapters."""

    TTS = "t"
    VIDEO = "v"

    @property
    def feature(self) -> Feature:
        return {
            PaidMediaApprovalKind.TTS: Feature.TTS,
            PaidMediaApprovalKind.VIDEO: Feature.VIDEO_GENERATE,
        }[self]


class PaidMediaApprovalAction(StrEnum):
    """Compact user decisions that fit Telegram's callback data limit."""

    RUN = "r"
    CANCEL = "c"
    USE_PERSONAL_ONCE = "m"
    ALWAYS_HERE = "a"


class PaidMediaApprovalCallback(CallbackData, prefix="pm"):
    """Feature route, decision, and opaque server-authorized capability."""

    kind: PaidMediaApprovalKind
    action: PaidMediaApprovalAction
    token: Annotated[
        str,
        Field(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9_-]+$"),
    ]


@dataclass(frozen=True, slots=True)
class PaidMediaRunContext:
    """Stable command principal, topic, and delivery destination."""

    requester_id: uuid.UUID
    requester_telegram_id: int
    chat_id: uuid.UUID
    chat_telegram_id: int
    message_id: int
    thread_id: int | None
    business_connection_id: str | None

    def __post_init__(self) -> None:
        for name in ("requester_id", "chat_id"):
            if not isinstance(getattr(self, name), uuid.UUID):
                raise TypeError(f"{name} must be a UUID")
        if (
            isinstance(self.requester_telegram_id, bool)
            or not isinstance(self.requester_telegram_id, int)
            or self.requester_telegram_id <= 0
        ):
            raise ValueError("requester_telegram_id must be positive")
        if (
            isinstance(self.chat_telegram_id, bool)
            or not isinstance(self.chat_telegram_id, int)
            or self.chat_telegram_id == 0
        ):
            raise ValueError("chat_telegram_id must not be zero")
        if (
            isinstance(self.message_id, bool)
            or not isinstance(self.message_id, int)
            or self.message_id <= 0
        ):
            raise ValueError("message_id must be positive")
        if self.thread_id is not None and (
            isinstance(self.thread_id, bool)
            or not isinstance(self.thread_id, int)
            or self.thread_id <= 0
        ):
            raise ValueError("thread_id must be positive")
        if self.business_connection_id is not None and not (
            isinstance(self.business_connection_id, str)
            and self.business_connection_id.strip()
        ):
            raise ValueError("business_connection_id must not be blank")

    @classmethod
    def from_snapshot(
        cls,
        snapshot: DeferredToolSnapshot,
        *,
        business_connection_id: str | None,
    ) -> PaidMediaRunContext:
        """Rebuild only scope facts authenticated by the approval capability."""
        return cls(
            requester_id=snapshot.requester_id,
            requester_telegram_id=snapshot.requester_telegram_id,
            chat_id=snapshot.chat_id,
            chat_telegram_id=snapshot.chat_telegram_id,
            message_id=snapshot.message_id,
            thread_id=snapshot.thread_id,
            business_connection_id=business_connection_id,
        )

    def invocation(
        self,
        feature: Feature,
        *,
        estimated_input_tokens: int,
        request_binding: str,
    ) -> PaidMediaInvocation:
        """Build the deterministic operation and original command reply target."""
        operation_id = OperationId.for_command(
            feature=feature,
            chat_id=self.chat_telegram_id,
            message_id=self.message_id,
        )
        return PaidMediaInvocation(
            operation_id=operation_id,
            request_key=f"paid-media-command:v1:{operation_id}",
            requester_id=self.requester_id,
            chat_id=self.chat_id,
            thread_id=self.thread_id,
            target=DeliveryTarget(
                chat_id=self.chat_telegram_id,
                thread_id=self.thread_id,
                reply_to_message_id=self.message_id,
                business_connection_id=self.business_connection_id,
            ),
            estimated_input_tokens=estimated_input_tokens,
            request_binding=request_binding,
        )


class PaidMediaApprovalAdapter[RequestT](Protocol):
    """Feature adapter for validated arguments, pricing, and provider execution."""

    kind: PaidMediaApprovalKind
    tool_name: str
    plan: ExecutionPlan

    def parse_tool_call(self, tool_call: ToolCallPart) -> RequestT:
        """Validate persisted arguments without external effects."""
        ...

    def quote_input(self, request: RequestT) -> PaidMediaQuoteInput:
        """Return the complete provider-billable input for this request."""
        ...

    async def execute(
        self,
        plan: ExecutionPlan,
        request: RequestT,
    ) -> Outcome[PaidMediaResult]:
        """Execute the provider-neutral feature service."""
        ...


@dataclass(frozen=True, slots=True)
class PreparedPaidMediaApproval:
    """One exact quote paired with its durable callback capability."""

    handle: DeferredToolHandle
    quote: Quote

    def __post_init__(self) -> None:
        if self.handle.snapshot.operation_id != self.quote.operation_id:
            raise ValueError("approval handle and quote must identify one operation")


@dataclass(frozen=True, slots=True)
class PaidMediaApprovalClaim:
    """Approval decision plus an optional exclusive resume lease."""

    decision: ApprovalDecision
    claim: ResumeClaim | None

    def __post_init__(self) -> None:
        if self.decision.disposition is DecisionDisposition.EXPIRED:
            if self.claim is not None:
                raise ValueError("expired approval cannot have a resume claim")
        elif self.claim is None:
            raise ValueError("live approval requires a resume claim")


@dataclass(frozen=True, slots=True)
class PaidMediaResumeResult:
    """Durable operation outcome paired with the currently owned resume lease."""

    outcome: PaidMediaOperationOutcome
    lease: ResumeLease


class PaidMediaApprovalCoordinator:
    """Bind deferred approval state to one idempotent paid-media operation."""

    def __init__(
        self,
        operations: PaidMediaOperationCoordinator,
        approvals: DeferredToolApprovalService,
        ledger: OperationLedger,
        request_binder: OperationRequestBinder,
    ) -> None:
        self._operations = operations
        self._approvals = approvals
        self._ledger = ledger
        self._request_binder = request_binder

    async def prepare[RequestT](
        self,
        *,
        context: PaidMediaRunContext,
        tool_call: ToolCallPart,
        original_history: Sequence[ModelMessage],
        adapter: PaidMediaApprovalAdapter[RequestT],
    ) -> PreparedPaidMediaApproval:
        """Quote first, then persist the exact validated arguments and history."""
        self._require_adapter(adapter)
        request = adapter.parse_tool_call(tool_call)
        quote_input = adapter.quote_input(request)
        invocation = context.invocation(
            adapter.plan.feature,
            estimated_input_tokens=quote_input.input_tokens,
            request_binding=self._request_binding(adapter, tool_call),
        )
        quote = await self._operations.ensure_quote(
            invocation,
            adapter.plan,
            quote_input,
        )
        handle = await self._approvals.create_request(
            operation_id=invocation.operation_id,
            quote_id=quote.id,
            message_id=context.message_id,
            tool_call=tool_call,
            original_history=durable_message_history(original_history),
        )
        return PreparedPaidMediaApproval(handle, quote)

    async def deny(self, capability: ApprovalCapability) -> ApprovalDecision:
        """Persist cancellation and close the still-unclaimed operation quote."""
        decision = await self._approvals.deny(capability)
        if decision.disposition is not DecisionDisposition.EXPIRED:
            try:
                await self._ledger.cancel(
                    decision.snapshot.operation_id,
                    reason="approval_denied",
                )
            except Exception as exc:
                report_exception(
                    "paid_media_approval_cancel_reconciliation_failed",
                    exception=exc,
                    level="warning",
                    request_id=str(decision.snapshot.request_id),
                    operation_id=str(decision.snapshot.operation_id),
                )
        return decision

    async def cancel(self, capability: ApprovalCapability) -> ApprovalDecision:
        """Cancel pending or approved pre-execution work without racing a run."""
        try:
            return await self.deny(capability)
        except ApprovalDecisionConflictError:
            claim = await self._approvals.claim_resume(capability)
            if not isinstance(claim, ResumeLease):
                raise ApprovalDecisionConflictError(
                    "paid-media approval is running or already terminal"
                ) from None

        try:
            await self._ledger.cancel(
                claim.snapshot.operation_id,
                reason="approval_denied",
            )
        except InvalidOperationTransitionError as exc:
            await self._approvals.release_resume(claim)
            raise ApprovalDecisionConflictError(
                "paid-media operation already started"
            ) from exc
        except BaseException:
            await self._approvals.release_resume(claim)
            raise

        snapshot = await self._approvals.cancel_resume(claim)
        return ApprovalDecision(
            ApprovalChoice.DENY,
            DecisionDisposition.APPLIED,
            snapshot,
        )

    async def approve_and_claim(
        self,
        capability: ApprovalCapability,
    ) -> PaidMediaApprovalClaim:
        """Approve idempotently, then atomically claim one bounded resume lease."""
        decision = await self._approvals.approve(capability)
        if decision.disposition is DecisionDisposition.EXPIRED:
            return PaidMediaApprovalClaim(decision, None)
        return PaidMediaApprovalClaim(
            decision,
            await self._approvals.claim_resume(capability),
        )

    async def release(self, lease: ResumeLease) -> bool:
        """Release retryable work without changing the approval decision."""
        return await self._approvals.release_resume(lease)

    async def complete(self, lease: ResumeLease) -> DeferredToolSnapshot:
        """Scrub persisted request content after a durable operation outcome."""
        return await self._approvals.mark_resumed(lease)

    async def reconcile_expired(
        self,
        snapshot: DeferredToolSnapshot,
    ) -> PaidMediaOperationOutcome:
        """Render financial truth when approval state expires before delivery."""
        try:
            await self._ledger.cancel(
                snapshot.operation_id,
                reason="approval_expired",
            )
        except InvalidOperationTransitionError:
            return await self._operations.resume_existing(snapshot.operation_id)
        return PaidMediaNotCharged(
            snapshot.operation_id,
            PaidMediaNotChargedReason.QUOTE_EXPIRED,
        )

    async def grant_personal_consent(
        self,
        snapshot: DeferredToolSnapshot,
        *,
        requester_id: uuid.UUID,
        chat_id: uuid.UUID,
    ) -> None:
        """Persist always-here consent only for the authenticated approval owner."""
        if requester_id != snapshot.requester_id or chat_id != snapshot.chat_id:
            raise PaidMediaApprovalError(
                "personal consent scope does not match the approval"
            )
        await self._ledger.grant_personal_consent(requester_id, chat_id)

    async def resume[RequestT](
        self,
        *,
        lease: ResumeLease,
        context: PaidMediaRunContext,
        adapter: PaidMediaApprovalAdapter[RequestT],
        allow_personal_once: bool = False,
        progress: ProgressReporter | None = None,
    ) -> PaidMediaResumeResult:
        """Rebuild provider input only from the authenticated persisted lease."""
        self._require_adapter(adapter)
        self._require_context(lease.snapshot, context)
        if lease.snapshot.tool_name != adapter.tool_name:
            raise PaidMediaApprovalError(
                "persisted tool does not match the paid-media adapter"
            )
        run_input = lease.build_run_input()
        tool_call = self._persisted_tool_call(
            run_input.message_history,
            snapshot=lease.snapshot,
        )
        request = adapter.parse_tool_call(tool_call)
        quote_input = adapter.quote_input(request)
        invocation = context.invocation(
            adapter.plan.feature,
            estimated_input_tokens=quote_input.input_tokens,
            request_binding=self._request_binding(adapter, tool_call),
        )
        if invocation.operation_id != lease.snapshot.operation_id:
            raise PaidMediaApprovalError(
                "persisted approval and command derive different operations"
            )

        lease_state = [lease]
        try:
            if progress is not None:
                await progress(ProgressStage.PREPARING)

            async def execute(
                plan: ExecutionPlan,
                provider_request: RequestT,
            ) -> Outcome[PaidMediaResult]:
                if progress is not None:
                    await progress(ProgressStage.GENERATING)
                outcome = await adapter.execute(plan, provider_request)
                if progress is not None and isinstance(outcome, Succeeded):
                    await progress(ProgressStage.DELIVERING)
                return outcome

            outcome = await self._run_with_lease_renewal(
                lease_state,
                self._operations.run(
                    invocation,
                    adapter.plan,
                    quote_input,
                    request,
                    execute,
                    allow_personal_once=allow_personal_once,
                ),
            )
        except BaseException:
            try:
                await self._approvals.release_resume(lease_state[0])
            except Exception as release_error:
                report_exception(
                    "paid_media_approval_lease_release_failed",
                    exception=release_error,
                    level="warning",
                    request_id=str(lease_state[0].snapshot.request_id),
                    operation_id=str(lease_state[0].snapshot.operation_id),
                )
            raise
        return PaidMediaResumeResult(outcome, lease_state[0])

    async def _run_with_lease_renewal[T](
        self,
        lease_state: list[ResumeLease],
        operation: Awaitable[T],
    ) -> T:
        """Keep one long provider run exclusively leased until it settles."""
        finished = asyncio.Event()

        async def renew() -> None:
            while True:
                lease = lease_state[0]
                delay = (lease.lease_expires_at - lease.claimed_at).total_seconds() / 2
                if delay <= 0:
                    raise PaidMediaApprovalError("resume lease duration is invalid")
                try:
                    await asyncio.wait_for(finished.wait(), timeout=delay)
                    return
                except TimeoutError:
                    lease_state[0] = await self._approvals.renew_resume(lease)

        operation_task = asyncio.create_task(operation)
        renewal_task = asyncio.create_task(renew())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {operation_task, renewal_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if operation_task in done:
                    finished.set()
                    await renewal_task
                    return await operation_task
                await renewal_task
        finally:
            finished.set()
            for task in (operation_task, renewal_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                operation_task,
                renewal_task,
                return_exceptions=True,
            )

    @staticmethod
    def _require_adapter(adapter: PaidMediaApprovalAdapter[object]) -> None:
        if adapter.plan.feature is not adapter.kind.feature:
            raise PaidMediaApprovalError(
                "paid-media adapter kind and execution plan do not match"
            )
        if not adapter.tool_name.strip():
            raise PaidMediaApprovalError("paid-media adapter tool name is blank")

    @staticmethod
    def _require_context(
        snapshot: DeferredToolSnapshot,
        context: PaidMediaRunContext,
    ) -> None:
        if (
            context.requester_id != snapshot.requester_id
            or context.requester_telegram_id != snapshot.requester_telegram_id
            or context.chat_id != snapshot.chat_id
            or context.chat_telegram_id != snapshot.chat_telegram_id
            or context.message_id != snapshot.message_id
            or context.thread_id != snapshot.thread_id
        ):
            raise PaidMediaApprovalError(
                "runtime context does not match the authenticated approval"
            )

    @staticmethod
    def _persisted_tool_call(
        history: Sequence[ModelMessage],
        *,
        snapshot: DeferredToolSnapshot,
    ) -> ToolCallPart:
        if not history or not isinstance(history[-1], ModelResponse):
            raise PaidMediaApprovalError(
                "deferred media history has no final model response"
            )
        calls = [
            part
            for part in history[-1].parts
            if isinstance(part, ToolCallPart)
            and part.tool_name == snapshot.tool_name
            and part.tool_call_id == snapshot.tool_call_id
        ]
        if len(calls) != 1:
            raise PaidMediaApprovalError(
                "deferred media tool identity is missing or ambiguous"
            )
        return calls[0]

    def _request_binding(
        self,
        adapter: PaidMediaApprovalAdapter[object],
        tool_call: ToolCallPart,
    ) -> str:
        """Bind canonical validated args without persisting reversible content."""
        try:
            arguments = tool_call.args_as_dict(raise_if_invalid=True)
        except Exception:
            raise PaidMediaApprovalError(
                "paid-media arguments cannot be bound"
            ) from None
        return self._request_binder.bind(
            adapter.plan.feature,
            {
                "tool_name": tool_call.tool_name,
                "args": arguments,
            },
        )


def approval_callback_context(
    callback: CallbackQuery,
    token: str,
) -> tuple[Message, ApprovalCapability]:
    """Build capability scope exclusively from Telegram's callback principal."""
    if callback.from_user is None or not isinstance(callback.message, Message):
        raise PaidMediaApprovalError("approval callback has no message context")
    message = callback.message
    return message, ApprovalCapability(
        token=token,
        requester_telegram_id=callback.from_user.id,
        chat_telegram_id=message.chat.id,
        thread_id=message.message_thread_id,
    )


def pack_paid_media_callback(
    kind: PaidMediaApprovalKind,
    action: PaidMediaApprovalAction,
    token: str,
) -> str:
    """Pack one callback and enforce Telegram's byte limit at construction."""
    packed = PaidMediaApprovalCallback(
        kind=kind,
        action=action,
        token=token,
    ).pack()
    if len(packed.encode("utf-8")) > 64:  # pragma: no cover - aiogram also guards
        raise ValueError("paid-media approval callback exceeds 64 bytes")
    return packed


__all__ = [
    "PaidMediaApprovalAction",
    "PaidMediaApprovalAdapter",
    "PaidMediaApprovalCallback",
    "PaidMediaApprovalClaim",
    "PaidMediaApprovalCoordinator",
    "PaidMediaApprovalError",
    "PaidMediaApprovalKind",
    "PaidMediaResumeResult",
    "PaidMediaRunContext",
    "PreparedPaidMediaApproval",
    "ProgressReporter",
    "approval_callback_context",
    "pack_paid_media_callback",
]
