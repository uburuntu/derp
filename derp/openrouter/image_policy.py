"""Live ZDR and price attestation for dedicated OpenRouter image routes."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from decimal import Decimal
from typing import Final, Protocol

from derp.catalog import ImagePricing, ImageResolution, InferenceProvider
from derp.execution import ExecutionPlan
from derp.openrouter.types import ImageModelEndpoints, ZdrEndpoints

DEFAULT_IMAGE_ATTESTATION_TTL: Final = timedelta(minutes=2)
_ONE_MILLION = Decimal(1_000_000)
_RESOLUTIONS = {
    ImageResolution.HALF_K: "512",
    ImageResolution.ONE_K: "1K",
    ImageResolution.TWO_K: "2K",
    ImageResolution.FOUR_K: "4K",
}


class ImageRoutePolicyClient(Protocol):
    """Metadata reads required before sending private image content."""

    async def get_image_model_endpoints(self, model: str) -> ImageModelEndpoints: ...

    async def list_zdr_endpoints(self) -> ZdrEndpoints: ...


class OpenRouterImageRouteGuard:
    """Fail closed unless the pinned endpoint is live, ZDR, and within price."""

    def __init__(
        self,
        client: ImageRoutePolicyClient,
        *,
        provider_tag: str,
        ttl: timedelta = DEFAULT_IMAGE_ATTESTATION_TTL,
        monotonic_clock=time.monotonic,
    ) -> None:
        if not callable(getattr(client, "get_image_model_endpoints", None)):
            raise TypeError("client must provide image endpoint metadata")
        if not callable(getattr(client, "list_zdr_endpoints", None)):
            raise TypeError("client must provide ZDR endpoint metadata")
        if not provider_tag.strip():
            raise ValueError("provider_tag must not be blank")
        if not timedelta(0) < ttl <= timedelta(minutes=10):
            raise ValueError("ttl must be positive and at most ten minutes")
        if not callable(monotonic_clock):
            raise TypeError("monotonic_clock must be callable")
        self._client = client
        self._provider_tag = provider_tag
        self._ttl_seconds = ttl.total_seconds()
        self._clock = monotonic_clock
        self._attested_key: tuple[str, ImageResolution, int] | None = None
        self._attested_until = 0.0
        self._lock = asyncio.Lock()

    async def allows(
        self,
        plan: ExecutionPlan,
        *,
        resolution: ImageResolution,
        input_reference_count: int,
    ) -> bool:
        """Attest the exact route without receiving prompt or media content."""
        if plan.model.provider is not InferenceProvider.OPENROUTER:
            return False
        if not isinstance(resolution, ImageResolution):
            raise TypeError("resolution must be an ImageResolution")
        if (
            isinstance(input_reference_count, bool)
            or not isinstance(input_reference_count, int)
            or input_reference_count < 0
        ):
            raise ValueError("input_reference_count must be non-negative")
        attestation_key = (
            plan.model.provider_model_id,
            resolution,
            input_reference_count,
        )
        now = self._clock()
        if self._attested_key == attestation_key and now < self._attested_until:
            return True

        async with self._lock:
            now = self._clock()
            if self._attested_key == attestation_key and now < self._attested_until:
                return True
            image_endpoints, zdr_endpoints = await asyncio.gather(
                self._client.get_image_model_endpoints(plan.model.provider_model_id),
                self._client.list_zdr_endpoints(),
            )
            allowed = self._validate(
                plan,
                image_endpoints=image_endpoints,
                zdr_endpoints=zdr_endpoints,
                resolution=resolution,
                input_reference_count=input_reference_count,
            )
            if allowed:
                self._attested_key = attestation_key
                self._attested_until = now + self._ttl_seconds
            return allowed

    def _validate(
        self,
        plan: ExecutionPlan,
        *,
        image_endpoints: ImageModelEndpoints,
        zdr_endpoints: ZdrEndpoints,
        resolution: ImageResolution,
        input_reference_count: int,
    ) -> bool:
        model = plan.model
        pricing = model.pricing
        if (
            not isinstance(pricing, ImagePricing)
            or image_endpoints.id != model.provider_model_id
        ):
            return False
        endpoint = next(
            (
                candidate
                for candidate in image_endpoints.endpoints
                if candidate.provider_tag == self._provider_tag
                and candidate.provider_slug == self._provider_tag
            ),
            None,
        )
        if endpoint is None:
            return False
        resolution_parameter = endpoint.supported_parameters.get("resolution")
        if (
            resolution_parameter is None
            or _RESOLUTIONS[resolution] not in resolution_parameter.values
        ):
            return False
        count_parameter = endpoint.supported_parameters.get("n")
        if (
            count_parameter is None
            or count_parameter.min is None
            or count_parameter.max is None
            or not count_parameter.min <= 1 <= count_parameter.max
        ):
            return False
        if input_reference_count:
            reference_parameter = endpoint.supported_parameters.get("input_references")
            if (
                reference_parameter is None
                or reference_parameter.max is None
                or input_reference_count > reference_parameter.max
            ):
                return False

        token_ceiling = pricing.output_token_per_million / _ONE_MILLION
        endpoint_prices = tuple(
            price.cost_usd
            for price in endpoint.pricing
            if price.billable == "output_image" and price.unit == "token"
        )
        if not endpoint_prices or max(endpoint_prices) > token_ceiling:
            return False

        zdr = next(
            (
                candidate
                for candidate in zdr_endpoints.data
                if candidate.model_id == model.provider_model_id
                and candidate.tag == self._provider_tag
            ),
            None,
        )
        if zdr is None or zdr.status != 0:
            return False
        expected = {
            "prompt": pricing.input_per_million / _ONE_MILLION,
            "completion": pricing.text_output_per_million / _ONE_MILLION,
            "image_output": token_ceiling,
        }
        return all(
            (live := getattr(zdr.pricing, name)) is not None and live <= ceiling
            for name, ceiling in expected.items()
        )


__all__ = [
    "DEFAULT_IMAGE_ATTESTATION_TTL",
    "ImageRoutePolicyClient",
    "OpenRouterImageRouteGuard",
]
