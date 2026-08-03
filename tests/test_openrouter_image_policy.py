"""Live image route admission is ZDR, capability, and price bounded."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from derp.catalog import ImageResolution, ModelRole
from derp.execution import Feature, plan_execution
from derp.openrouter import (
    OPENROUTER_IMAGE_VERTEX_ENDPOINT,
    ImageModelEndpoint,
    ImageModelEndpoints,
    OpenRouterImageRouteGuard,
    ZdrEndpoint,
    ZdrEndpoints,
)
from derp.openrouter.types import (
    ImageEndpointParameter,
    ImageEndpointPrice,
    ZdrEndpointPricing,
)


def _metadata(*, output_unit_cost: str = "0.00006") -> tuple[object, object]:
    model_id = "google/gemini-3.1-flash-image"
    image = ImageModelEndpoints(
        id=model_id,
        endpoints=(
            ImageModelEndpoint(
                provider_name="Google Vertex",
                provider_slug=OPENROUTER_IMAGE_VERTEX_ENDPOINT,
                provider_tag=OPENROUTER_IMAGE_VERTEX_ENDPOINT,
                supported_parameters={
                    "resolution": ImageEndpointParameter(
                        type="enum", values=("512", "1K", "2K", "4K")
                    ),
                    "n": ImageEndpointParameter(type="range", min=1, max=1),
                    "input_references": ImageEndpointParameter(
                        type="range", min=0, max=14
                    ),
                },
                pricing=(
                    ImageEndpointPrice(
                        billable="output_image",
                        unit="token",
                        cost_usd=output_unit_cost,
                    ),
                ),
            ),
        ),
    )
    zdr = ZdrEndpoints(
        data=(
            ZdrEndpoint(
                model_id=model_id,
                provider_name="Google",
                tag=OPENROUTER_IMAGE_VERTEX_ENDPOINT,
                status=0,
                pricing=ZdrEndpointPricing(
                    prompt="0.0000005",
                    completion="0.000003",
                    image_output=output_unit_cost,
                ),
            ),
        )
    )
    return image, zdr


@pytest.mark.asyncio
async def test_guard_attests_and_caches_exact_private_image_route() -> None:
    image, zdr = _metadata()
    client = SimpleNamespace(
        get_image_model_endpoints=AsyncMock(return_value=image),
        list_zdr_endpoints=AsyncMock(return_value=zdr),
    )
    clock = SimpleNamespace(now=10.0)
    guard = OpenRouterImageRouteGuard(
        client,
        provider_tag=OPENROUTER_IMAGE_VERTEX_ENDPOINT,
        monotonic_clock=lambda: clock.now,
    )
    plan = plan_execution(Feature.IMAGE_EDIT, ModelRole.IMAGE)

    assert await guard.allows(
        plan,
        resolution=ImageResolution.FOUR_K,
        input_reference_count=1,
    )
    clock.now += 30
    assert await guard.allows(
        plan,
        resolution=ImageResolution.FOUR_K,
        input_reference_count=1,
    )

    client.get_image_model_endpoints.assert_awaited_once_with(
        plan.model.provider_model_id
    )
    client.list_zdr_endpoints.assert_awaited_once()


@pytest.mark.asyncio
async def test_guard_rejects_live_price_drift_without_caching_it() -> None:
    image, zdr = _metadata(output_unit_cost="0.000061")
    client = SimpleNamespace(
        get_image_model_endpoints=AsyncMock(return_value=image),
        list_zdr_endpoints=AsyncMock(return_value=zdr),
    )
    guard = OpenRouterImageRouteGuard(
        client,
        provider_tag=OPENROUTER_IMAGE_VERTEX_ENDPOINT,
    )

    assert not await guard.allows(
        plan_execution(Feature.IMAGE_GENERATE, ModelRole.IMAGE),
        resolution=ImageResolution.ONE_K,
        input_reference_count=0,
    )


@pytest.mark.asyncio
async def test_guard_does_not_reuse_generation_attestation_for_an_edit() -> None:
    image, zdr = _metadata()
    image.endpoints[0].supported_parameters.pop("input_references")
    client = SimpleNamespace(
        get_image_model_endpoints=AsyncMock(return_value=image),
        list_zdr_endpoints=AsyncMock(return_value=zdr),
    )
    guard = OpenRouterImageRouteGuard(
        client,
        provider_tag=OPENROUTER_IMAGE_VERTEX_ENDPOINT,
    )

    assert await guard.allows(
        plan_execution(Feature.IMAGE_GENERATE, ModelRole.IMAGE),
        resolution=ImageResolution.ONE_K,
        input_reference_count=0,
    )
    assert not await guard.allows(
        plan_execution(Feature.IMAGE_EDIT, ModelRole.IMAGE),
        resolution=ImageResolution.ONE_K,
        input_reference_count=1,
    )
    assert client.get_image_model_endpoints.await_count == 2
