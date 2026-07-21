"""Paid-operation telemetry stays numeric, consistent, and content-free."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from derp.catalog import GoogleModelKey
from derp.execution import ExecutionPlan, Feature, plan_execution
from derp.operations import (
    ChatQuoteInput,
    FundingAuthorization,
    OperationId,
    Quote,
    QuoteEngine,
    QuoteId,
    record_operation_outcome,
    record_operation_quote,
)


def _quote() -> tuple[ExecutionPlan, Quote]:
    plan = plan_execution(Feature.CHAT, GoogleModelKey.CHAT_STANDARD)
    quote = QuoteEngine().quote(
        quote_id=QuoteId.new(),
        operation_id=OperationId.for_command(
            feature=Feature.CHAT,
            chat_id=-100,
            message_id=1,
        ),
        plan=plan,
        quote_input=ChatQuoteInput(2_000),
        created_at=datetime(2026, 7, 21, tzinfo=UTC),
    )
    return plan, quote


def test_quote_and_outcome_use_one_cross_feature_vocabulary() -> None:
    plan, quote = _quote()
    span = MagicMock()

    record_operation_quote(
        span,
        quote,
        provider_model_id=plan.model.provider_model_id,
    )
    record_operation_outcome(
        span,
        "delivered",
        authorization=FundingAuthorization.CHAT,
        terminal_outcome="captured",
    )

    economics = span.set_attributes.call_args_list[0].args[0]
    assert economics["derp.operation.capability"] == "chat"
    assert economics["derp.operation.quoted_credits"] == quote.credits
    assert isinstance(economics["derp.operation.estimated_provider_cost_usd"], float)
    assert span.set_attributes.call_args_list[1].args[0] == {
        "derp.operation.authorization": "chat",
        "derp.operation.outcome": "delivered",
        "derp.operation.terminal_outcome": "captured",
    }


@pytest.mark.parametrize("label", ["", "user said hello", "prompt=<private>", "x" * 81])
def test_outcome_labels_reject_content_shaped_values(label: str) -> None:
    with pytest.raises(ValueError, match="controlled telemetry label"):
        record_operation_outcome(MagicMock(), label)


def test_quote_rejects_missing_provider_identity() -> None:
    _, quote = _quote()
    with pytest.raises(ValueError, match="provider_model_id"):
        record_operation_quote(MagicMock(), quote, provider_model_id=" ")
