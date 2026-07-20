"""Minimal typed vocabulary shared by planning, execution, and settlement."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from derp.catalog import (
    GoogleModelKey,
    GoogleModelSpec,
    ModelCapability,
    get_google_model,
)


class Feature(StrEnum):
    """Provider-backed product capabilities with distinct execution policy."""

    CHAT = "chat"
    INLINE_CHAT = "inline_chat"
    DEEP_THINK = "deep_think"
    IMAGE_GENERATE = "image_generate"
    IMAGE_EDIT = "image_edit"
    TTS = "tts"
    VIDEO_GENERATE = "video_generate"


_REQUIRED_CAPABILITIES: Mapping[Feature, frozenset[ModelCapability]] = MappingProxyType(
    {
        Feature.CHAT: frozenset(
            {
                ModelCapability.TEXT_INPUT,
                ModelCapability.TEXT_OUTPUT,
                ModelCapability.IMAGE_INPUT,
                ModelCapability.AUDIO_INPUT,
                ModelCapability.VIDEO_INPUT,
                ModelCapability.PDF_INPUT,
                ModelCapability.TOOLS,
            }
        ),
        Feature.INLINE_CHAT: frozenset(
            {ModelCapability.TEXT_INPUT, ModelCapability.TEXT_OUTPUT}
        ),
        Feature.DEEP_THINK: frozenset(
            {
                ModelCapability.TEXT_INPUT,
                ModelCapability.TEXT_OUTPUT,
                ModelCapability.THINKING,
            }
        ),
        Feature.IMAGE_GENERATE: frozenset(
            {ModelCapability.TEXT_INPUT, ModelCapability.IMAGE_OUTPUT}
        ),
        Feature.IMAGE_EDIT: frozenset(
            {
                ModelCapability.TEXT_INPUT,
                ModelCapability.IMAGE_INPUT,
                ModelCapability.IMAGE_OUTPUT,
            }
        ),
        Feature.TTS: frozenset(
            {ModelCapability.TEXT_INPUT, ModelCapability.AUDIO_OUTPUT}
        ),
        Feature.VIDEO_GENERATE: frozenset(
            {
                ModelCapability.TEXT_INPUT,
                ModelCapability.IMAGE_INPUT,
                ModelCapability.VIDEO_OUTPUT,
            }
        ),
    }
)


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """One feature bound to the exact catalog model used for execution."""

    feature: Feature
    model: GoogleModelSpec

    def __post_init__(self) -> None:
        if get_google_model(self.model.key) is not self.model:
            raise ValueError("Execution plans require the canonical catalog model")
        missing = _REQUIRED_CAPABILITIES[self.feature] - self.model.capabilities
        if missing:
            capabilities = ", ".join(sorted(capability.value for capability in missing))
            raise ValueError(
                f"{self.model.key.value} cannot execute {self.feature.value}; "
                f"missing {capabilities}"
            )


def plan_execution(
    feature: Feature,
    model: GoogleModelSpec | GoogleModelKey,
) -> ExecutionPlan:
    """Construct a validated plan without performing external effects."""
    spec = get_google_model(model) if isinstance(model, GoogleModelKey) else model
    return ExecutionPlan(feature=feature, model=spec)


_CURRENT_PLAN: ContextVar[ExecutionPlan | None] = ContextVar(
    "derp_execution_plan", default=None
)


@contextmanager
def execution_plan_scope(plan: ExecutionPlan | None) -> Iterator[None]:
    """Bind an access-selected plan to one provider tool invocation."""
    token = _CURRENT_PLAN.set(plan)
    try:
        yield
    finally:
        _CURRENT_PLAN.reset(token)


def require_execution_plan(feature: Feature) -> ExecutionPlan:
    """Return the scoped plan and verify that it belongs to the feature."""
    plan = _CURRENT_PLAN.get()
    if plan is None:
        raise RuntimeError(f"{feature.value} execution has no access-selected plan")
    if plan.feature is not feature:
        raise ValueError(f"{plan.feature.value} cannot execute {feature.value}")
    return plan


class RejectionReason(StrEnum):
    """Expected reasons provider output is not a successful result."""

    INVALID_INPUT = "invalid_input"
    POLICY = "policy"
    UNUSABLE_OUTPUT = "unusable_output"


class FailureReason(StrEnum):
    """Infrastructure reasons an execution did not produce an outcome."""

    PROVIDER_ERROR = "provider_error"


@dataclass(frozen=True, slots=True)
class Succeeded[T]:
    """A successful execution with its required domain payload."""

    value: T


@dataclass(frozen=True, slots=True)
class Rejected:
    """An expected rejection with no success payload."""

    reason: RejectionReason


@dataclass(frozen=True, slots=True)
class Failed:
    """An infrastructure failure with no success payload."""

    reason: FailureReason


type Outcome[T] = Succeeded[T] | Rejected | Failed


__all__ = [
    "ExecutionPlan",
    "Failed",
    "FailureReason",
    "Feature",
    "Outcome",
    "Rejected",
    "RejectionReason",
    "Succeeded",
    "execution_plan_scope",
    "plan_execution",
    "require_execution_plan",
]
