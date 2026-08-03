"""Provider-neutral image requests and bounded execution service."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

from derp.catalog import ImageResolution, InferenceProvider
from derp.execution import (
    ExecutionPlan,
    Failed,
    FailureReason,
    Feature,
    Outcome,
    Rejected,
    RejectionReason,
    Succeeded,
)
from derp.features.types import MediaContent
from derp.inference_types import InferenceReport
from derp.media.gateway import DEFAULT_ALLOWED_MIME_TYPES
from derp.media.types import MediaFamily, MediaReference, normalize_mime_type

MAX_IMAGE_PROMPT_CHARS = 8_000
MAX_IMAGE_STYLE_CHARS = 200
MAX_IMAGE_OUTPUT_BYTES = 10 * 1024 * 1024
V1_IMAGE_OUTPUT_COUNT = 1

_DEFAULT_INPUT_MIME_TYPES = DEFAULT_ALLOWED_MIME_TYPES[MediaFamily.IMAGE]
_DEFAULT_OUTPUT_MIME_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)


def _normalized_text(value: str, *, name: str, max_chars: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be blank")
    if len(normalized) > max_chars:
        raise ValueError(f"{name} must be at most {max_chars} characters")
    return normalized


def _require_resolution(value: ImageResolution) -> None:
    if not isinstance(value, ImageResolution):
        raise TypeError("resolution must be an ImageResolution")


@dataclass(frozen=True, slots=True)
class ImageGenerateRequest:
    """Validated provider-independent image generation request."""

    prompt: str
    style: str | None = None
    resolution: ImageResolution = ImageResolution.ONE_K

    def __post_init__(self) -> None:
        _require_resolution(self.resolution)
        object.__setattr__(
            self,
            "prompt",
            _normalized_text(
                self.prompt,
                name="image prompt",
                max_chars=MAX_IMAGE_PROMPT_CHARS,
            ),
        )
        if self.style is not None:
            object.__setattr__(
                self,
                "style",
                _normalized_text(
                    self.style,
                    name="image style",
                    max_chars=MAX_IMAGE_STYLE_CHARS,
                ),
            )


@dataclass(frozen=True, slots=True)
class ImageEditRequest:
    """Validated edit request retaining a durable source reference."""

    prompt: str
    source: MediaReference
    resolution: ImageResolution = ImageResolution.ONE_K

    def __post_init__(self) -> None:
        if not isinstance(self.source, MediaReference):
            raise TypeError("image edit source must be a MediaReference")
        _require_resolution(self.resolution)
        object.__setattr__(
            self,
            "prompt",
            _normalized_text(
                self.prompt,
                name="image edit prompt",
                max_chars=MAX_IMAGE_PROMPT_CHARS,
            ),
        )


@dataclass(frozen=True, slots=True)
class PreparedImageEditRequest:
    """Edit request after bounded source hydration at the feature boundary."""

    prompt: str
    source: MediaContent
    resolution: ImageResolution = ImageResolution.ONE_K

    def __post_init__(self) -> None:
        if not isinstance(self.source, MediaContent):
            raise TypeError("prepared image source must be MediaContent")
        if self.source.family is not MediaFamily.IMAGE:
            raise ValueError("image editing requires image source media")
        _require_resolution(self.resolution)


@dataclass(frozen=True, slots=True)
class ImageOutput:
    """Provider-independent images awaiting feature-policy validation."""

    images: tuple[MediaContent, ...]
    reports: tuple[InferenceReport, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.images, tuple):
            raise TypeError("image output must use an immutable tuple")
        if not self.images:
            raise ValueError("image output must contain at least one image")
        if any(not isinstance(image, MediaContent) for image in self.images):
            raise TypeError("image output entries must be MediaContent")
        if any(image.family is not MediaFamily.IMAGE for image in self.images):
            raise ValueError("image output may contain only image media")
        if not isinstance(self.reports, tuple):
            raise TypeError("image reports must use an immutable tuple")
        if any(not isinstance(report, InferenceReport) for report in self.reports):
            raise TypeError("image reports must contain InferenceReport values")


@dataclass(frozen=True, slots=True)
class ImageExecutionPolicy:
    """Resource and deadline limits enforced around every image provider."""

    max_source_bytes: int = 20_000_000
    max_output_bytes: int = MAX_IMAGE_OUTPUT_BYTES
    max_output_images: int = V1_IMAGE_OUTPUT_COUNT
    provider_deadline_seconds: float = 90.0
    allowed_input_mime_types: frozenset[str] = field(
        default_factory=lambda: _DEFAULT_INPUT_MIME_TYPES
    )
    allowed_output_mime_types: frozenset[str] = field(
        default_factory=lambda: _DEFAULT_OUTPUT_MIME_TYPES
    )

    def __post_init__(self) -> None:
        for name in (
            "max_source_bytes",
            "max_output_bytes",
            "max_output_images",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_output_bytes > MAX_IMAGE_OUTPUT_BYTES:
            raise ValueError(
                f"max_output_bytes must not exceed {MAX_IMAGE_OUTPUT_BYTES}"
            )
        if self.max_output_images != V1_IMAGE_OUTPUT_COUNT:
            raise ValueError(
                f"max_output_images must be {V1_IMAGE_OUTPUT_COUNT} for image v1"
            )
        if (
            not math.isfinite(self.provider_deadline_seconds)
            or self.provider_deadline_seconds <= 0
        ):
            raise ValueError("provider_deadline_seconds must be finite and positive")
        for name in ("allowed_input_mime_types", "allowed_output_mime_types"):
            values = frozenset(
                normalize_mime_type(value) for value in getattr(self, name)
            )
            if not values or any(not value.startswith("image/") for value in values):
                raise ValueError(f"{name} must contain image MIME types")
            object.__setattr__(self, name, values)


class ImageSourceLoader(Protocol):
    """Hydrate a stable reference through an owned bounded media adapter."""

    async def load(self, reference: MediaReference) -> bytes:
        """Return temporary source bytes for this execution only."""
        ...


class ImageProviderExecutor(Protocol):
    """Provider adapter that has no Telegram, database, or billing effects."""

    async def generate(
        self,
        plan: ExecutionPlan,
        request: ImageGenerateRequest,
    ) -> Outcome[ImageOutput]:
        """Generate image media using the exact validated execution plan."""
        ...

    async def edit(
        self,
        plan: ExecutionPlan,
        request: PreparedImageEditRequest,
    ) -> Outcome[ImageOutput]:
        """Edit bounded source media using the exact execution plan."""
        ...


class ImageProviderRouter:
    """Dispatch image work by the provider fixed in its execution plan."""

    def __init__(
        self,
        executors: Mapping[InferenceProvider, ImageProviderExecutor],
    ) -> None:
        if not executors:
            raise ValueError("at least one image provider executor is required")
        for provider, executor in executors.items():
            if not isinstance(provider, InferenceProvider):
                raise TypeError("image executor keys must be InferenceProvider values")
            if not callable(getattr(executor, "generate", None)) or not callable(
                getattr(executor, "edit", None)
            ):
                raise TypeError("image executors must implement generate and edit")
        self._executors = MappingProxyType(dict(executors))

    async def generate(
        self,
        plan: ExecutionPlan,
        request: ImageGenerateRequest,
    ) -> Outcome[ImageOutput]:
        """Generate through the provider selected before billing."""
        return await self._executor(plan).generate(plan, request)

    async def edit(
        self,
        plan: ExecutionPlan,
        request: PreparedImageEditRequest,
    ) -> Outcome[ImageOutput]:
        """Edit through the provider selected before billing."""
        return await self._executor(plan).edit(plan, request)

    def _executor(self, plan: ExecutionPlan) -> ImageProviderExecutor:
        try:
            return self._executors[plan.model.provider]
        except KeyError:
            raise RuntimeError(
                f"no image executor configured for {plan.model.provider.value}"
            ) from None


class ImageFeatureService:
    """Validate resource bounds and execute one injected image provider."""

    def __init__(
        self,
        executor: ImageProviderExecutor,
        *,
        source_loader: ImageSourceLoader | None = None,
        policy: ImageExecutionPolicy | None = None,
    ) -> None:
        self._executor = executor
        self._source_loader = source_loader
        self._policy = policy or ImageExecutionPolicy()

    async def generate(
        self,
        plan: ExecutionPlan,
        request: ImageGenerateRequest,
    ) -> Outcome[ImageOutput]:
        """Generate images within the provider deadline and output budget."""
        self._require_feature(plan, Feature.IMAGE_GENERATE)
        return await self._run_provider(self._executor.generate(plan, request))

    async def edit(
        self,
        plan: ExecutionPlan,
        request: ImageEditRequest,
    ) -> Outcome[ImageOutput]:
        """Hydrate one bounded source image, then execute the edit provider."""
        self._require_feature(plan, Feature.IMAGE_EDIT)
        if self._source_loader is None:
            raise RuntimeError("image editing requires a source loader")
        metadata = request.source.metadata
        if metadata.mime_type not in self._policy.allowed_input_mime_types:
            return Rejected(RejectionReason.INVALID_INPUT)
        if (
            metadata.file_size is not None
            and metadata.file_size > self._policy.max_source_bytes
        ):
            return Rejected(RejectionReason.INVALID_INPUT)

        try:
            source_bytes = await self._source_loader.load(request.source)
        except Exception:
            return Failed(FailureReason.PROVIDER_ERROR)
        if not isinstance(source_bytes, bytes):
            return Failed(FailureReason.PROVIDER_ERROR)
        if not source_bytes or len(source_bytes) > self._policy.max_source_bytes:
            return Rejected(RejectionReason.INVALID_INPUT)

        prepared = PreparedImageEditRequest(
            prompt=request.prompt,
            resolution=request.resolution,
            source=MediaContent(
                family=MediaFamily.IMAGE,
                mime_type=metadata.mime_type,
                data=source_bytes,
            ),
        )
        return await self._run_provider(self._executor.edit(plan, prepared))

    async def _run_provider(
        self,
        execution: Awaitable[Outcome[ImageOutput]],
    ) -> Outcome[ImageOutput]:
        try:
            async with asyncio.timeout(self._policy.provider_deadline_seconds):
                outcome = await execution
        except TimeoutError:
            return Failed(FailureReason.PROVIDER_ERROR)
        except Exception:
            return Failed(FailureReason.PROVIDER_ERROR)
        if isinstance(outcome, (Rejected, Failed)):
            return outcome
        if not isinstance(outcome, Succeeded):
            return Failed(FailureReason.PROVIDER_ERROR)
        if not isinstance(outcome.value, ImageOutput):
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)
        images = outcome.value.images
        if len(images) != self._policy.max_output_images:
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)
        if any(
            image.mime_type not in self._policy.allowed_output_mime_types
            for image in images
        ):
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)
        if any(image.size_bytes > self._policy.max_output_bytes for image in images):
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)
        return outcome

    @staticmethod
    def _require_feature(plan: ExecutionPlan, expected: Feature) -> None:
        if plan.feature is not expected:
            raise ValueError(f"{plan.feature.value} cannot execute {expected.value}")


__all__ = [
    "MAX_IMAGE_PROMPT_CHARS",
    "MAX_IMAGE_STYLE_CHARS",
    "MAX_IMAGE_OUTPUT_BYTES",
    "V1_IMAGE_OUTPUT_COUNT",
    "ImageEditRequest",
    "ImageExecutionPolicy",
    "ImageFeatureService",
    "ImageGenerateRequest",
    "ImageOutput",
    "ImageProviderExecutor",
    "ImageProviderRouter",
    "ImageSourceLoader",
    "PreparedImageEditRequest",
]
