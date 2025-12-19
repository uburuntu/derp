"""Guardrail tests for credit system.

These tests ensure:
- Model pricing follows expected tier hierarchy
- Credit costs are computed correctly from pricing
- Tools require appropriate credits
"""

from __future__ import annotations

from derp.credits.models import (
    MODEL_REGISTRY,
    ModelConfig,
    ModelTier,
    ModelType,
    calculate_credit_cost,
    get_default_model,
)
from derp.credits.tools import TOOL_REGISTRY, get_tool


class TestModelPricingGuardrails:
    """Ensure expensive models can't be used cheaply."""

    def test_all_models_have_positive_credit_cost(self) -> None:
        """Every model must cost at least 1 credit."""
        for model in MODEL_REGISTRY.values():
            assert model.credit_cost >= 1, f"{model.id} has credit_cost < 1"

    def test_premium_costs_more_than_standard(self) -> None:
        """Premium tier must cost significantly more than standard."""
        premium = get_default_model(ModelType.TEXT, ModelTier.PREMIUM)
        standard = get_default_model(ModelType.TEXT, ModelTier.STANDARD)

        # Premium should be at least 3x standard
        assert premium.credit_cost >= standard.credit_cost * 3, (
            f"Premium ({premium.credit_cost}) should be >= 3x Standard ({standard.credit_cost})"
        )

    def test_standard_costs_more_than_cheap(self) -> None:
        """Standard tier must cost more than cheap tier."""
        standard = get_default_model(ModelType.TEXT, ModelTier.STANDARD)
        cheap = get_default_model(ModelType.TEXT, ModelTier.CHEAP)

        # Standard should be at least 2x cheap
        assert standard.credit_cost >= cheap.credit_cost * 2, (
            f"Standard ({standard.credit_cost}) should be >= 2x Cheap ({cheap.credit_cost})"
        )

    def test_tier_hierarchy_for_text_models(self) -> None:
        """Text models should follow CHEAP < STANDARD < PREMIUM."""
        cheap = get_default_model(ModelType.TEXT, ModelTier.CHEAP)
        standard = get_default_model(ModelType.TEXT, ModelTier.STANDARD)
        premium = get_default_model(ModelType.TEXT, ModelTier.PREMIUM)

        assert cheap.credit_cost < standard.credit_cost < premium.credit_cost, (
            f"Tier hierarchy violated: CHEAP({cheap.credit_cost}) < "
            f"STANDARD({standard.credit_cost}) < PREMIUM({premium.credit_cost})"
        )

    def test_credit_cost_reflects_actual_cost(self) -> None:
        """Credit cost should match calculated cost from pricing (within tolerance)."""
        for model in MODEL_REGISTRY.values():
            calculated = calculate_credit_cost(model)
            # Allow 20% tolerance for any manual adjustments
            ratio = model.credit_cost / calculated if calculated > 0 else 1.0
            assert 0.8 <= ratio <= 1.2, (
                f"{model.id}: credit_cost={model.credit_cost}, calculated={calculated}"
            )

    def test_image_models_have_per_request_cost(self) -> None:
        """Image models should have per-request cost (not token-based)."""
        image_models = [
            m for m in MODEL_REGISTRY.values() if m.model_type == ModelType.IMAGE
        ]
        for model in image_models:
            # Either per_request_cost or token costs should be set
            has_pricing = (
                model.per_request_cost > 0
                or model.input_cost_per_1m > 0
                or model.output_cost_per_1m > 0
            )
            assert has_pricing, f"Image model {model.id} has no pricing"


class TestModelRegistryCompleteness:
    """Ensure registry has all required defaults."""

    def test_text_tiers_have_defaults(self) -> None:
        """All text tiers must have a default model."""
        for tier in [ModelTier.CHEAP, ModelTier.STANDARD, ModelTier.PREMIUM]:
            model = get_default_model(ModelType.TEXT, tier)
            assert model is not None, f"No default text model for {tier}"
            assert model.is_default, f"{model.id} not marked as default"

    def test_image_has_default(self) -> None:
        """Image generation must have a default model."""
        model = get_default_model(ModelType.IMAGE, ModelTier.STANDARD)
        assert model is not None, "No default image model"

    def test_no_duplicate_defaults(self) -> None:
        """Each type+tier combination should have exactly one default."""
        defaults: dict[tuple[ModelType, ModelTier], list[str]] = {}
        for model in MODEL_REGISTRY.values():
            if model.is_default:
                key = (model.model_type, model.tier)
                defaults.setdefault(key, []).append(model.id)

        for key, models in defaults.items():
            assert len(models) == 1, f"Multiple defaults for {key}: {models}"


class TestToolRegistryGuardrails:
    """Ensure tools have sensible configurations."""

    def test_all_tools_have_valid_model_type(self) -> None:
        """Every tool must reference a valid model type."""
        for tool in TOOL_REGISTRY.values():
            assert tool.model_type in ModelType, f"{tool.name} has invalid model_type"

    def test_premium_tools_flagged_correctly(self) -> None:
        """Premium tools should have is_premium=True."""
        for tool in TOOL_REGISTRY.values():
            # If base_credit_cost > 0 and free_daily_limit == 0, should be premium
            if tool.base_credit_cost > 0 and tool.free_daily_limit == 0:
                assert tool.is_premium, (
                    f"{tool.name} has cost but no free limit, should be is_premium=True"
                )

    def test_free_tools_have_no_base_cost(self) -> None:
        """Tools with high free limits shouldn't have base cost."""
        for tool in TOOL_REGISTRY.values():
            # If free_daily_limit is high (>10), base_credit_cost should be 0
            if tool.free_daily_limit > 10:
                assert tool.base_credit_cost == 0, (
                    f"{tool.name} has high free limit ({tool.free_daily_limit}) "
                    f"but charges {tool.base_credit_cost} credits"
                )

    def test_default_models_exist(self) -> None:
        """If a tool specifies a default model, it must exist."""
        for tool in TOOL_REGISTRY.values():
            if tool.default_model_id:
                assert tool.default_model_id in MODEL_REGISTRY, (
                    f"{tool.name} references non-existent model {tool.default_model_id}"
                )

    def test_tool_model_type_matches_default(self) -> None:
        """Default model type should match tool's model_type."""
        for tool in TOOL_REGISTRY.values():
            if tool.default_model_id:
                model = MODEL_REGISTRY[tool.default_model_id]
                assert model.model_type == tool.model_type, (
                    f"{tool.name} has model_type={tool.model_type} but "
                    f"default model {model.id} is {model.model_type}"
                )


class TestCreditCostCalculation:
    """Test the credit cost calculation function."""

    def test_zero_cost_returns_minimum(self) -> None:
        """Models with zero pricing should still cost 1 credit."""
        model = ModelConfig(
            id="test-free",
            provider="test",
            display_name="Test Free",
            model_type=ModelType.TEXT,
            tier=ModelTier.CHEAP,
            # No pricing set
        )
        assert model.credit_cost == 1

    def test_expensive_model_costs_more(self) -> None:
        """Higher API costs should result in higher credit costs."""
        cheap = ModelConfig(
            id="test-cheap",
            provider="test",
            display_name="Test Cheap",
            model_type=ModelType.TEXT,
            tier=ModelTier.CHEAP,
            input_cost_per_1m=0,
            output_cost_per_1m=0,
            per_request_cost=0,
        )
        expensive = ModelConfig(
            id="test-expensive",
            provider="test",
            display_name="Test Expensive",
            model_type=ModelType.TEXT,
            tier=ModelTier.PREMIUM,
            input_cost_per_1m=10,
            output_cost_per_1m=30,
        )
        assert expensive.credit_cost > cheap.credit_cost

    def test_per_request_cost_factored_in(self) -> None:
        """Per-request costs should be included in credit calculation."""
        base = ModelConfig(
            id="test-base",
            provider="test",
            display_name="Test Base",
            model_type=ModelType.IMAGE,
            tier=ModelTier.STANDARD,
            per_request_cost=0,
        )
        with_cost = ModelConfig(
            id="test-with-cost",
            provider="test",
            display_name="Test With Cost",
            model_type=ModelType.IMAGE,
            tier=ModelTier.STANDARD,
            per_request_cost=100,  # $0.10 per request
        )
        assert with_cost.credit_cost > base.credit_cost


class TestToolCostCalculation:
    """Test tool total cost calculation."""

    def test_total_cost_includes_model(self) -> None:
        """Tool's total_cost should add base + model cost."""
        tool = get_tool("image_generate")
        model = MODEL_REGISTRY[tool.default_model_id]

        expected = tool.base_credit_cost + model.credit_cost
        assert tool.total_cost(model.credit_cost) == expected

    def test_free_tool_still_charges_model(self) -> None:
        """Free tools (base_credit_cost=0) should still account for model cost."""
        tool = get_tool("web_search")
        assert tool.base_credit_cost == 0

        # When used with a model that costs 5 credits
        assert tool.total_cost(5) == 5


class TestCreditCheckResult:
    """Test CreditCheckResult type behavior."""

    def test_free_use_properties(self) -> None:
        """Free tier uses should have correct properties."""
        from derp.credits.types import CreditCheckResult

        result = CreditCheckResult(
            allowed=True,
            tier=ModelTier.CHEAP,
            model_id="test",
            source="free",
            credits_to_deduct=0,
            credits_remaining=None,
            free_remaining=5,
        )
        assert result.is_free_use
        assert not result.is_paid
        assert result.allowed

    def test_paid_use_properties(self) -> None:
        """Paid uses should have correct properties."""
        from derp.credits.types import CreditCheckResult

        result = CreditCheckResult(
            allowed=True,
            tier=ModelTier.STANDARD,
            model_id="test",
            source="chat",
            credits_to_deduct=10,
            credits_remaining=90,
            free_remaining=None,
        )
        assert not result.is_free_use
        assert result.is_paid
        assert result.allowed

    def test_rejected_has_reason(self) -> None:
        """Rejected results should have a reason."""
        from derp.credits.types import CreditCheckResult

        result = CreditCheckResult(
            allowed=False,
            tier=ModelTier.STANDARD,
            model_id="test",
            source="rejected",
            credits_to_deduct=0,
            credits_remaining=0,
            free_remaining=0,
            reject_reason="Not enough credits",
        )
        assert not result.allowed
        assert result.reject_reason == "Not enough credits"


class TestContextLimits:
    """Test context limit configuration."""

    def test_context_limits_defined(self) -> None:
        """All tiers should have context limits defined."""
        from derp.credits.service import CONTEXT_LIMITS

        assert ModelTier.CHEAP in CONTEXT_LIMITS
        assert ModelTier.STANDARD in CONTEXT_LIMITS
        assert ModelTier.PREMIUM in CONTEXT_LIMITS

    def test_cheap_has_lower_context(self) -> None:
        """Cheap tier should have lower context limit."""
        from derp.credits.service import CONTEXT_LIMITS

        assert CONTEXT_LIMITS[ModelTier.CHEAP] < CONTEXT_LIMITS[ModelTier.STANDARD]

    def test_context_limits_positive(self) -> None:
        """All context limits should be positive."""
        from derp.credits.service import CONTEXT_LIMITS

        for tier, limit in CONTEXT_LIMITS.items():
            assert limit > 0, f"{tier} has non-positive context limit: {limit}"
