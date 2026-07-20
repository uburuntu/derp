"""Immutable, versioned Stars products used by intent snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from derp.billing.types import ProductKind, UnknownProductError

TELEGRAM_SUBSCRIPTION_PERIOD_SECONDS: Final = 30 * 24 * 60 * 60
PRODUCT_VERSION: Final = "2026-07-20-v1"


@dataclass(frozen=True, slots=True)
class TopUpProduct:
    """One non-expiring credit pack."""

    id: str
    version: str
    name: str
    stars: int
    credits: int
    currency: str = "XTR"
    kind: ProductKind = ProductKind.TOP_UP

    def __post_init__(self) -> None:
        _validate_product(self.id, self.version, self.name, self.stars, self.credits)
        if self.currency != "XTR":
            raise ValueError("Stars products must use XTR")


@dataclass(frozen=True, slots=True)
class SubscriptionPlan:
    """The single renewable personal allowance plan."""

    id: str
    version: str
    name: str
    stars: int
    allowance_credits: int
    currency: str = "XTR"
    period_seconds: int = TELEGRAM_SUBSCRIPTION_PERIOD_SECONDS
    kind: ProductKind = ProductKind.SUBSCRIPTION

    def __post_init__(self) -> None:
        _validate_product(
            self.id,
            self.version,
            self.name,
            self.stars,
            self.allowance_credits,
        )
        if self.currency != "XTR":
            raise ValueError("Stars products must use XTR")
        if self.period_seconds != TELEGRAM_SUBSCRIPTION_PERIOD_SECONDS:
            raise ValueError("Telegram subscriptions must use a 30-day period")

    @property
    def credits(self) -> int:
        return self.allowance_credits


type StarsProduct = TopUpProduct | SubscriptionPlan


@dataclass(frozen=True, slots=True)
class ProductCatalog:
    """Version-addressable top-ups plus exactly one personal plan."""

    top_ups: tuple[TopUpProduct, ...]
    subscription_plan: SubscriptionPlan

    def __post_init__(self) -> None:
        keys = [(item.kind, item.id, item.version) for item in self.top_ups]
        if not self.top_ups or len(keys) != len(set(keys)):
            raise ValueError("top-up product versions must be present and unique")

    def resolve(self, kind: ProductKind, product_id: str, version: str) -> StarsProduct:
        if kind is ProductKind.SUBSCRIPTION:
            plan = self.subscription_plan
            if (plan.id, plan.version) == (product_id, version):
                return plan
        else:
            for product in self.top_ups:
                if (product.id, product.version) == (product_id, version):
                    return product
        raise UnknownProductError(f"Unknown {kind.value} product version")

    @property
    def current_top_ups(self) -> MappingProxyType[str, TopUpProduct]:
        return MappingProxyType({item.id: item for item in self.top_ups})


def _validate_product(
    product_id: str,
    version: str,
    name: str,
    stars: int,
    credit_count: int,
) -> None:
    for field_name, value in (
        ("id", product_id),
        ("version", version),
        ("name", name),
    ):
        if not value.strip():
            raise ValueError(f"product {field_name} must not be blank")
    if stars <= 0 or credit_count <= 0:
        raise ValueError("product Stars and credits must be positive")


DEFAULT_PRODUCT_CATALOG: Final = ProductCatalog(
    top_ups=(
        TopUpProduct("starter", PRODUCT_VERSION, "Starter", 50, 50),
        TopUpProduct("basic", PRODUCT_VERSION, "Basic", 150, 165),
        TopUpProduct("standard", PRODUCT_VERSION, "Standard", 500, 600),
        TopUpProduct("bulk", PRODUCT_VERSION, "Bulk", 1_500, 2_000),
    ),
    subscription_plan=SubscriptionPlan(
        id="personal_monthly",
        version=PRODUCT_VERSION,
        name="Derp Personal",
        stars=500,
        allowance_credits=1_000,
    ),
)


__all__ = [
    "DEFAULT_PRODUCT_CATALOG",
    "PRODUCT_VERSION",
    "TELEGRAM_SUBSCRIPTION_PERIOD_SECONDS",
    "ProductCatalog",
    "StarsProduct",
    "SubscriptionPlan",
    "TopUpProduct",
]
