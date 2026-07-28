"""Pure contracts for versioned Stars products and opaque payloads."""

from dataclasses import FrozenInstanceError
from datetime import date
from decimal import Decimal

import pytest

from derp.billing import (
    DEFAULT_PRODUCT_CATALOG,
    DEFAULT_STAR_ECONOMICS,
    PRODUCT_VERSION,
    TELEGRAM_ECONOMICS_VERIFIED_ON,
    TELEGRAM_PRIVATE_TOPICS_FEE_URL,
    TELEGRAM_STARS_GUIDE_URL,
    TELEGRAM_STARS_REWARDS_URL,
    ProductCatalog,
    ProductKind,
    SubscriptionPlan,
    TopUpProduct,
    UnknownProductError,
)
from derp.billing.payloads import generate_invoice_payload, hash_invoice_payload


def test_default_catalog_has_versioned_topups_and_one_personal_plan() -> None:
    catalog = DEFAULT_PRODUCT_CATALOG

    assert PRODUCT_VERSION == "2026-07-28-v1"
    assert {
        product.id: (product.stars, product.credits) for product in catalog.top_ups
    } == {
        "starter": (50, 600),
        "standard": (250, 3_200),
        "bulk": (750, 9_900),
    }
    assert (catalog.subscription_plan.stars, catalog.subscription_plan.credits) == (
        500,
        6_750,
    )
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


def test_release_economics_use_dated_official_telegram_assumptions() -> None:
    economics = DEFAULT_STAR_ECONOMICS

    assert economics.verified_on == TELEGRAM_ECONOMICS_VERIFIED_ON == date(2026, 7, 28)
    assert economics.official_reward_per_star_usd == Decimal("0.013")
    assert economics.private_topics_fee_rate == Decimal("0.15")
    assert economics.reward_per_star_usd(private_topics_enabled=True) == Decimal(
        "0.01105"
    )
    assert economics.planning_revenue_floor_per_star_usd == Decimal("0.01")
    assert economics.provider_liability_per_credit_usd == Decimal("0.0007")
    assert economics.source_urls == (
        TELEGRAM_STARS_REWARDS_URL,
        TELEGRAM_PRIVATE_TOPICS_FEE_URL,
        TELEGRAM_STARS_GUIDE_URL,
    )
    assert all(url.startswith("https://") for url in economics.source_urls)


@pytest.mark.parametrize(
    ("product_id", "expected_liability", "expected_margin"),
    [
        ("starter", Decimal("0.4200"), Decimal("0.16")),
        ("standard", Decimal("2.2400"), Decimal("0.104")),
        ("bulk", Decimal("6.9300"), Decimal("0.076")),
        ("personal_monthly", Decimal("4.7250"), Decimal("0.055")),
    ],
)
def test_release_products_preserve_margin_at_the_planning_floor(
    product_id: str,
    expected_liability: Decimal,
    expected_margin: Decimal,
) -> None:
    catalog = DEFAULT_PRODUCT_CATALOG
    product = (
        catalog.subscription_plan
        if product_id == catalog.subscription_plan.id
        else catalog.current_top_ups[product_id]
    )

    result = catalog.economics.evaluate(
        stars=product.stars,
        credit_count=product.credits,
    )

    assert result.worst_case_credit_liability_usd == expected_liability
    assert result.planning_margin == expected_margin
    assert result.planning_revenue_usd >= result.worst_case_credit_liability_usd


def test_catalog_rejects_product_liability_above_the_planning_floor() -> None:
    version = "unsafe-v1"

    with pytest.raises(ValueError, match="liability exceeds"):
        ProductCatalog(
            top_ups=(TopUpProduct("unsafe", version, "Unsafe", 1, 15),),
            subscription_plan=SubscriptionPlan(
                "plan",
                version,
                "Plan",
                1,
                10,
            ),
            debug_top_up=TopUpProduct("debug", version, "Debug", 1, 10),
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
