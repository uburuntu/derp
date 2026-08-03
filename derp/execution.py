"""Minimal typed vocabulary shared by planning, execution, and settlement."""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from derp.catalog import (
    InferenceProvider,
    ModelCapability,
    ModelRole,
    ModelSpec,
    get_google_model,
    get_openrouter_model,
)


class Feature(StrEnum):
    """Provider-backed product capabilities with distinct execution policy."""

    CHAT = "chat"
    INLINE_CHAT = "inline_chat"
    DEEP_THINK = "deep_think"
    IMAGE_GENERATE = "image_generate"
    IMAGE_EDIT = "image_edit"
    TTS = "tts"
    TRANSCRIBE = "transcribe"
    VIDEO_GENERATE = "video_generate"


_REQUIRED_CAPABILITIES: Mapping[Feature, frozenset[ModelCapability]] = MappingProxyType(
    {
        Feature.CHAT: frozenset(
            {
                ModelCapability.TEXT_INPUT,
                ModelCapability.TEXT_OUTPUT,
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
        Feature.TRANSCRIBE: frozenset(
            {ModelCapability.AUDIO_INPUT, ModelCapability.TEXT_OUTPUT}
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

_MODEL_ROLES_BY_FEATURE: Mapping[Feature, frozenset[ModelRole]] = MappingProxyType(
    {
        Feature.CHAT: frozenset(
            {
                ModelRole.CHAT_ECONOMY,
                ModelRole.CHAT_STANDARD,
                ModelRole.CHAT_MULTIMODAL,
                ModelRole.FREE_TEXT,
                ModelRole.FREE_VISUAL,
                ModelRole.FREE_AUDIO,
            }
        ),
        Feature.INLINE_CHAT: frozenset({ModelRole.CHAT_ECONOMY, ModelRole.FREE_TEXT}),
        Feature.DEEP_THINK: frozenset({ModelRole.CHAT_REASONING}),
        Feature.IMAGE_GENERATE: frozenset({ModelRole.IMAGE}),
        Feature.IMAGE_EDIT: frozenset({ModelRole.IMAGE}),
        Feature.TTS: frozenset({ModelRole.TTS}),
        Feature.TRANSCRIBE: frozenset({ModelRole.STT}),
        Feature.VIDEO_GENERATE: frozenset(
            {ModelRole.VIDEO_FAST, ModelRole.VIDEO_STANDARD}
        ),
    }
)


def model_roles_for_features(features: Collection[Feature]) -> tuple[ModelRole, ...]:
    """Return deterministic catalog roles reachable through selected features."""
    if any(not isinstance(feature, Feature) for feature in features):
        raise TypeError("features must contain only Feature values")
    enabled = {
        role for feature in features for role in _MODEL_ROLES_BY_FEATURE[feature]
    }
    return tuple(role for role in ModelRole if role in enabled)


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """One feature bound to the exact catalog model used for execution."""

    feature: Feature
    model: ModelSpec

    def __post_init__(self) -> None:
        resolver = (
            get_openrouter_model
            if self.model.provider is InferenceProvider.OPENROUTER
            else get_google_model
        )
        if resolver(self.model.key) is not self.model:
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
    model: ModelSpec | ModelRole,
    *,
    provider: InferenceProvider = InferenceProvider.OPENROUTER,
) -> ExecutionPlan:
    """Construct a validated plan without performing external effects."""
    if isinstance(model, ModelRole):
        resolver = (
            get_openrouter_model
            if provider is InferenceProvider.OPENROUTER
            else get_google_model
        )
        spec = resolver(model)
    else:
        spec = model
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
    "model_roles_for_features",
    "plan_execution",
    "require_execution_plan",
]
