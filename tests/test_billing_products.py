"""Pure contracts for versioned Stars products and opaque payloads."""

from dataclasses import FrozenInstanceError

import pytest

from derp.billing import DEFAULT_PRODUCT_CATALOG, ProductKind, UnknownProductError
from derp.billing.payloads import generate_invoice_payload, hash_invoice_payload


def test_default_catalog_has_versioned_topups_and_one_personal_plan() -> None:
    catalog = DEFAULT_PRODUCT_CATALOG

    assert set(catalog.current_top_ups) == {"starter", "basic", "standard", "bulk"}
    assert catalog.debug_top_up.id not in catalog.current_top_ups
    assert catalog.debug_top_up.stars == 1
    assert (
        catalog.resolve(
            ProductKind.TOP_UP,
            catalog.debug_top_up.id,
            catalog.debug_top_up.version,
        )
        is catalog.debug_top_up
    )
    assert all(product.kind is ProductKind.TOP_UP for product in catalog.top_ups)
    assert catalog.subscription_plan.kind is ProductKind.SUBSCRIPTION
    assert catalog.subscription_plan.period_seconds == 30 * 24 * 60 * 60
    assert (
        catalog.resolve(
            ProductKind.SUBSCRIPTION,
            catalog.subscription_plan.id,
            catalog.subscription_plan.version,
        )
        is catalog.subscription_plan
    )


def test_products_are_immutable_and_unknown_versions_fail_closed() -> None:
    product = DEFAULT_PRODUCT_CATALOG.current_top_ups["starter"]

    with pytest.raises(FrozenInstanceError):
        product.stars = 1  # type: ignore[misc]
    with pytest.raises(UnknownProductError):
        DEFAULT_PRODUCT_CATALOG.resolve(ProductKind.TOP_UP, product.id, "retired")


def test_invoice_payload_is_opaque_bounded_and_only_its_hash_is_durable() -> None:
    payload = generate_invoice_payload(lambda: "test-token")
    digest = hash_invoice_payload(payload)

    assert payload == "dpi1_test-token"
    assert len(payload.encode()) <= 128
    assert len(digest) == 64
    assert payload not in digest


@pytest.mark.parametrize("token", ["", "not ascii \u2603", "has spaces"])
def test_invalid_invoice_tokens_fail_before_persistence(token: str) -> None:
    with pytest.raises(ValueError):
        generate_invoice_payload(lambda: token)
