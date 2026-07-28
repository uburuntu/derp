"""OpenRouter adapter for provider-neutral image execution."""

from __future__ import annotations

import base64
from typing import Protocol

from derp.catalog import (
    DataCollectionPolicy,
    ImageResolution,
    InferenceProvider,
    ModelRole,
)
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
from derp.features import (
    ImageGenerateRequest,
    ImageOutput,
    MediaContent,
    PreparedImageEditRequest,
)
from derp.inference_types import InferenceReport
from derp.inference_usage import InferenceTokenUsage
from derp.media import MediaFamily
from derp.observability import report_exception
from derp.openrouter.errors import OpenRouterError
from derp.openrouter.types import (
    ImageGenerationRequest as OpenRouterImageRequest,
)
from derp.openrouter.types import (
    ImageGenerationResult,
    ImageOutputFormat,
    MediaReference,
    MediaReferenceKind,
    ProviderRouting,
)

OPENROUTER_IMAGE_MODEL = "google/gemini-3.1-flash-image"
OPENROUTER_IMAGE_CANONICAL_MODEL = "google/gemini-3.1-flash-image-20260528"
# A full provider tag prevents future Vertex variants from widening this route.
OPENROUTER_IMAGE_VERTEX_ENDPOINT = "google-vertex/global"

_CATALOG_VERTEX_ROUTE = "google-vertex"
_PINNED_PROVIDER_ROUTING = ProviderRouting(
    order=(OPENROUTER_IMAGE_VERTEX_ENDPOINT,),
    only=(OPENROUTER_IMAGE_VERTEX_ENDPOINT,),
    allow_fallbacks=False,
)
_RESOLUTIONS = {
    ImageResolution.HALF_K: "512",
    ImageResolution.ONE_K: "1K",
    ImageResolution.TWO_K: "2K",
    ImageResolution.FOUR_K: "4K",
}


class OpenRouterImageClient(Protocol):
    """Narrow typed transport surface consumed by the image adapter."""

    async def generate_image(
        self,
        request: OpenRouterImageRequest,
    ) -> ImageGenerationResult: ...


class OpenRouterImageRoutePolicy(Protocol):
    """Live, content-free admission check for one private image request."""

    async def allows(
        self,
        plan: ExecutionPlan,
        *,
        resolution: ImageResolution,
        input_reference_count: int,
    ) -> bool: ...


class OpenRouterImageExecutor:
    """Execute one image request through the reviewed private Vertex route."""

    def __init__(
        self,
        client: OpenRouterImageClient,
        route_policy: OpenRouterImageRoutePolicy,
    ) -> None:
        if not callable(getattr(client, "generate_image", None)):
            raise TypeError("client must support image generation")
        if not callable(getattr(route_policy, "allows", None)):
            raise TypeError("route_policy must support live admission")
        self._client = client
        self._route_policy = route_policy

    async def generate(
        self,
        plan: ExecutionPlan,
        request: ImageGenerateRequest,
    ) -> Outcome[ImageOutput]:
        """Generate one image using the exact catalog-selected model."""
        self._require_executor_plan(plan, Feature.IMAGE_GENERATE)
        if not self._has_reviewed_private_route(plan):
            return Rejected(RejectionReason.POLICY)
        if not await self._live_route_allowed(
            plan,
            resolution=request.resolution,
            input_reference_count=0,
        ):
            return Rejected(RejectionReason.POLICY)
        prompt = request.prompt
        if request.style:
            prompt = f"{prompt}\n\nStyle: {request.style}"
        provider_request = self._request(
            model=plan.model.provider_model_id,
            prompt=prompt,
            resolution=request.resolution,
        )
        return await self._execute(provider_request)

    async def edit(
        self,
        plan: ExecutionPlan,
        request: PreparedImageEditRequest,
    ) -> Outcome[ImageOutput]:
        """Edit one bounded source image through the same pinned endpoint."""
        self._require_executor_plan(plan, Feature.IMAGE_EDIT)
        if not self._has_reviewed_private_route(plan):
            return Rejected(RejectionReason.POLICY)
        if not await self._live_route_allowed(
            plan,
            resolution=request.resolution,
            input_reference_count=1,
        ):
            return Rejected(RejectionReason.POLICY)
        encoded = base64.b64encode(request.source.data).decode("ascii")
        reference = MediaReference(
            kind=MediaReferenceKind.IMAGE,
            url=f"data:{request.source.mime_type};base64,{encoded}",
        )
        provider_request = self._request(
            model=plan.model.provider_model_id,
            prompt=request.prompt,
            resolution=request.resolution,
            references=(reference,),
        )
        return await self._execute(provider_request)

    async def _execute(
        self,
        request: OpenRouterImageRequest,
    ) -> Outcome[ImageOutput]:
        try:
            result = await self._client.generate_image(request)
        except OpenRouterError:
            return Failed(FailureReason.PROVIDER_ERROR)

        if len(result.images) != 1:
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)
        image = result.images[0]
        try:
            reports = self._reports(result, request.model)
            output = ImageOutput(
                images=(
                    MediaContent(
                        family=MediaFamily.IMAGE,
                        mime_type=image.media_type or "image/png",
                        data=image.data,
                    ),
                ),
                reports=reports,
            )
        except TypeError, ValueError:
            return Rejected(RejectionReason.UNUSABLE_OUTPUT)
        return Succeeded(output)

    async def _live_route_allowed(
        self,
        plan: ExecutionPlan,
        *,
        resolution: ImageResolution,
        input_reference_count: int,
    ) -> bool:
        try:
            return await self._route_policy.allows(
                plan,
                resolution=resolution,
                input_reference_count=input_reference_count,
            )
        except Exception as exc:
            report_exception(
                "openrouter.image_route_attestation_failed",
                exception=exc,
                level="warning",
                model=plan.model.provider_model_id,
            )
            return False

    @staticmethod
    def _reports(
        result: ImageGenerationResult,
        requested_model: str,
    ) -> tuple[InferenceReport, ...]:
        usage = result.usage
        if usage is None and result.generation_id is None:
            return ()
        prompt_details = usage and usage.prompt_tokens_details
        completion_details = usage and usage.completion_tokens_details
        return (
            InferenceReport(
                provider="openrouter",
                requested_model=requested_model,
                actual_model=requested_model,
                downstream_provider=OPENROUTER_IMAGE_VERTEX_ENDPOINT,
                provider_response_id=None,
                generation_id=result.generation_id,
                finish_reason=None,
                tokens=(
                    InferenceTokenUsage(
                        input_tokens=usage.prompt_tokens,
                        output_tokens=usage.completion_tokens,
                        total_tokens=usage.total_tokens,
                        cache_read_tokens=(
                            prompt_details.cached_tokens if prompt_details else 0
                        )
                        or 0,
                        cache_write_tokens=(
                            prompt_details.cache_write_tokens if prompt_details else 0
                        )
                        or 0,
                        reasoning_tokens=(
                            completion_details.reasoning_tokens
                            if completion_details
                            else 0
                        )
                        or 0,
                        audio_input_tokens=(
                            prompt_details.audio_tokens if prompt_details else 0
                        )
                        or 0,
                    )
                    if usage is not None
                    else None
                ),
                actual_cost_usd=usage and usage.cost,
            ),
        )

    @staticmethod
    def _request(
        *,
        model: str,
        prompt: str,
        resolution: ImageResolution,
        references: tuple[MediaReference, ...] = (),
    ) -> OpenRouterImageRequest:
        return OpenRouterImageRequest(
            model=model,
            prompt=prompt,
            n=1,
            resolution=_RESOLUTIONS[resolution],
            output_format=ImageOutputFormat.PNG,
            input_references=references,
            provider=_PINNED_PROVIDER_ROUTING,
        )

    @staticmethod
    def _require_executor_plan(plan: ExecutionPlan, expected: Feature) -> None:
        if plan.feature is not expected:
            raise ValueError(f"{plan.feature.value} cannot execute {expected.value}")
        if plan.model.provider is not InferenceProvider.OPENROUTER:
            raise ValueError("OpenRouter image executor requires an OpenRouter plan")
        if plan.model.key is not ModelRole.IMAGE:
            raise ValueError("OpenRouter image executor requires the image model role")

    @staticmethod
    def _has_reviewed_private_route(plan: ExecutionPlan) -> bool:
        model = plan.model
        route = model.routing
        return (
            model.available
            and model.provider_model_id == OPENROUTER_IMAGE_MODEL
            and model.canonical_model_id == OPENROUTER_IMAGE_CANONICAL_MODEL
            and model.retention_exception is None
            and route is not None
            and route.require_parameters
            and route.data_collection is DataCollectionPolicy.DENY
            and route.zero_data_retention
            and route.max_price.image is not None
            and route.max_price.request is not None
            and route.provider_order
            in {
                (_CATALOG_VERTEX_ROUTE,),
                (OPENROUTER_IMAGE_VERTEX_ENDPOINT,),
            }
        )


__all__ = [
    "OPENROUTER_IMAGE_CANONICAL_MODEL",
    "OPENROUTER_IMAGE_MODEL",
    "OPENROUTER_IMAGE_VERTEX_ENDPOINT",
    "OpenRouterImageClient",
    "OpenRouterImageExecutor",
    "OpenRouterImageRoutePolicy",
]
