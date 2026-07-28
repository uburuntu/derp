"""Conservative Telegram Stars economics for release product pricing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from derp.catalog import CREDIT_BASE_USD, DEFAULT_MARGIN

TELEGRAM_ECONOMICS_VERIFIED_ON: Final = date(2026, 7, 28)
TELEGRAM_STARS_REWARDS_URL: Final = (
    "https://telegram.org/tos/bot-developers#6-2-4-rewards-for-stars"
)
TELEGRAM_PRIVATE_TOPICS_FEE_URL: Final = (
    "https://telegram.org/tos/bot-developers#6-2-6-enabling-topics-in-private-chats"
)
TELEGRAM_STARS_GUIDE_URL: Final = "https://core.telegram.org/bots/payments-stars"


def _positive_count(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _require_decimal(value: Decimal, name: str, *, positive: bool) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal")
    if not value.is_finite() or (positive and value <= 0):
        requirement = "positive and finite" if positive else "finite"
        raise ValueError(f"{name} must be {requirement}")
    return value


@dataclass(frozen=True, slots=True)
class StarsProductEconomics:
    """Auditable revenue and maximum provider liability for one product."""

    stars: int
    credits: int
    official_reward_usd: Decimal
    private_topics_reward_usd: Decimal
    planning_revenue_usd: Decimal
    worst_case_credit_liability_usd: Decimal
    planning_margin: Decimal

    def __post_init__(self) -> None:
        _positive_count(self.stars, "stars")
        _positive_count(self.credits, "credits")
        amounts = (
            self.official_reward_usd,
            self.private_topics_reward_usd,
            self.planning_revenue_usd,
            self.worst_case_credit_liability_usd,
        )
        for index, value in enumerate(amounts):
            _require_decimal(value, f"amount_{index}", positive=True)
        _require_decimal(self.planning_margin, "planning_margin", positive=False)
        if self.planning_margin >= 1:
            raise ValueError("planning margin must be less than one")


@dataclass(frozen=True, slots=True)
class StarEconomics:
    """Telegram rewards plus Derp's conservative commercial assumptions."""

    verified_on: date = TELEGRAM_ECONOMICS_VERIFIED_ON
    official_reward_per_star_usd: Decimal = Decimal("0.013")
    private_topics_fee_rate: Decimal = Decimal("0.15")
    planning_revenue_floor_per_star_usd: Decimal = Decimal("0.01")
    credit_value_usd: Decimal = CREDIT_BASE_USD
    quote_margin: Decimal = DEFAULT_MARGIN

    def __post_init__(self) -> None:
        if not isinstance(self.verified_on, date):
            raise TypeError("verified_on must be a date")
        for value, name in (
            (self.official_reward_per_star_usd, "official_reward_per_star_usd"),
            (
                self.planning_revenue_floor_per_star_usd,
                "planning_revenue_floor_per_star_usd",
            ),
            (self.credit_value_usd, "credit_value_usd"),
        ):
            _require_decimal(value, name, positive=True)
        for rate, name in (
            (self.private_topics_fee_rate, "private_topics_fee_rate"),
            (self.quote_margin, "quote_margin"),
        ):
            _require_decimal(rate, name, positive=False)
            if not Decimal(0) <= rate < Decimal(1):
                raise ValueError(f"{name} must be finite and in [0, 1)")
        if self.planning_revenue_floor_per_star_usd > self.reward_per_star_usd(
            private_topics_enabled=True
        ):
            raise ValueError("planning floor exceeds the post-fee official reward")

    @property
    def source_urls(self) -> tuple[str, str, str]:
        """Return the dated official sources behind the release assumptions."""
        return (
            TELEGRAM_STARS_REWARDS_URL,
            TELEGRAM_PRIVATE_TOPICS_FEE_URL,
            TELEGRAM_STARS_GUIDE_URL,
        )

    @property
    def provider_liability_per_credit_usd(self) -> Decimal:
        """Maximum provider cost represented by one quoted credit."""
        return self.credit_value_usd * (Decimal(1) - self.quote_margin)

    def reward_per_star_usd(self, *, private_topics_enabled: bool) -> Decimal:
        """Return Telegram's official reward with the private-topic fee applied."""
        if not isinstance(private_topics_enabled, bool):
            raise TypeError("private_topics_enabled must be a bool")
        if not private_topics_enabled:
            return self.official_reward_per_star_usd
        return self.official_reward_per_star_usd * (
            Decimal(1) - self.private_topics_fee_rate
        )

    def evaluate(self, *, stars: int, credit_count: int) -> StarsProductEconomics:
        """Evaluate one immutable product against the conservative planning floor."""
        stars = _positive_count(stars, "stars")
        credit_count = _positive_count(credit_count, "credit_count")
        official = self.official_reward_per_star_usd * stars
        post_fee = self.reward_per_star_usd(private_topics_enabled=True) * stars
        planning_revenue = self.planning_revenue_floor_per_star_usd * stars
        liability = self.provider_liability_per_credit_usd * credit_count
        margin = (planning_revenue - liability) / planning_revenue
        return StarsProductEconomics(
            stars=stars,
            credits=credit_count,
            official_reward_usd=official,
            private_topics_reward_usd=post_fee,
            planning_revenue_usd=planning_revenue,
            worst_case_credit_liability_usd=liability,
            planning_margin=margin,
        )


DEFAULT_STAR_ECONOMICS: Final = StarEconomics()


__all__ = [
    "DEFAULT_STAR_ECONOMICS",
    "TELEGRAM_ECONOMICS_VERIFIED_ON",
    "TELEGRAM_PRIVATE_TOPICS_FEE_URL",
    "TELEGRAM_STARS_GUIDE_URL",
    "TELEGRAM_STARS_REWARDS_URL",
    "StarEconomics",
    "StarsProductEconomics",
]
