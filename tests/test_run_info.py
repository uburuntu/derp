"""User-visible run receipt projections."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from derp.handlers.run_info import _render_run_info
from derp.inference_usage import InferenceTokenUsage
from derp.run_info import ChatRunDelivery, RunInfo, RunPrivacyMode
from derp.run_info.service import _aggregate_tokens, _charged_credits


def test_chat_run_delivery_rejects_missing_response_messages() -> None:
    with pytest.raises(ValueError, match="response_message_ids"):
        ChatRunDelivery(
            operation_id=uuid4(),
            chat_id=uuid4(),
            requester_id=uuid4(),
            request_message_id=1,
            response_message_ids=(),
            model_key="chat_standard",
            model_display_name="Claude Sonnet 5",
            privacy_mode=RunPrivacyMode.PRIVATE,
            context_messages=0,
            context_turns=0,
            context_estimated_tokens=0,
        )


def test_token_aggregation_preserves_every_reported_category() -> None:
    rows = (
        SimpleNamespace(
            usage_available=True,
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
            cache_read_tokens=50,
            cache_write_tokens=4,
            reasoning_tokens=8,
            audio_input_tokens=3,
            audio_output_tokens=2,
        ),
        SimpleNamespace(
            usage_available=True,
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            cache_read_tokens=0,
            cache_write_tokens=1,
            reasoning_tokens=2,
            audio_input_tokens=0,
            audio_output_tokens=0,
        ),
    )

    assert _aggregate_tokens(rows).total_tokens == 135
    assert _aggregate_tokens(rows).cache_read_tokens == 50
    assert _aggregate_tokens(rows).reasoning_tokens == 10


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("captured", 116),
        ("canceled", 0),
        ("released", 0),
        ("reversed", 0),
        ("executing", None),
    ],
)
def test_charged_credits_follow_the_settlement_state(
    state: str,
    expected: int | None,
) -> None:
    operation = SimpleNamespace(state=state)
    quote = SimpleNamespace(amount_credits=116)

    assert _charged_credits(operation, quote) == expected


def test_run_info_copy_is_useful_without_provider_economics() -> None:
    text = _render_run_info(
        RunInfo(
            model_display_name="Claude Sonnet 5",
            privacy_mode=RunPrivacyMode.PRIVATE,
            tokens=InferenceTokenUsage(
                input_tokens=1234,
                output_tokens=321,
                total_tokens=1555,
                cache_read_tokens=100,
                reasoning_tokens=20,
            ),
            context_messages=8,
            context_turns=4,
            context_estimated_tokens=980,
            charged_credits=116,
        )
    )

    assert "About this answer" in text
    assert "Claude Sonnet 5" in text
    assert "1234 in · 321 out" in text
    assert "~980 tokens · 8 messages" in text
    assert "116 credits" in text
    assert "USD" not in text
    assert "OpenRouter" not in text


def test_free_run_info_reports_zero_charge() -> None:
    text = _render_run_info(
        RunInfo(
            model_display_name="Gemini Flash",
            privacy_mode=RunPrivacyMode.FREE,
            tokens=None,
            context_messages=0,
            context_turns=0,
            context_estimated_tokens=0,
            charged_credits=0,
        )
    )

    assert "Free model" in text
    assert "Not reported" in text
    assert "0 credits" in text


def test_shared_run_info_hides_requester_charge() -> None:
    text = _render_run_info(
        RunInfo(
            model_display_name="Claude Sonnet 5",
            privacy_mode=RunPrivacyMode.PRIVATE,
            tokens=None,
            context_messages=1,
            context_turns=1,
            context_estimated_tokens=20,
            charged_credits=116,
            charge_visible=False,
        )
    )

    assert "Only the requester can see this" in text
    assert "116" not in text
