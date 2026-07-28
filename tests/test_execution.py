"""Contracts for the deliberately small execution vocabulary."""

from dataclasses import FrozenInstanceError, replace

import pytest

from derp.catalog import (
    GoogleModelKey,
    InferenceProvider,
    get_google_model,
    get_openrouter_model,
)
from derp.execution import (
    ExecutionPlan,
    Failed,
    FailureReason,
    Feature,
    Rejected,
    RejectionReason,
    Succeeded,
    execution_plan_scope,
    model_roles_for_features,
    plan_execution,
    require_execution_plan,
)


@pytest.mark.parametrize(
    ("feature", "model_key"),
    [
        (Feature.CHAT, GoogleModelKey.CHAT_STANDARD),
        (Feature.INLINE_CHAT, GoogleModelKey.CHAT_ECONOMY),
        (Feature.DEEP_THINK, GoogleModelKey.CHAT_REASONING),
        (Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE),
        (Feature.IMAGE_EDIT, GoogleModelKey.IMAGE),
        (Feature.TTS, GoogleModelKey.TTS),
        (Feature.VIDEO_GENERATE, GoogleModelKey.VIDEO_FAST),
        (Feature.VIDEO_GENERATE, GoogleModelKey.VIDEO_STANDARD),
    ],
)
def test_feature_plans_accept_current_catalog_models(
    feature: Feature,
    model_key: GoogleModelKey,
) -> None:
    model = get_google_model(model_key)
    plan = plan_execution(feature, model)

    assert plan.feature is feature
    assert plan.model is model


def test_plan_rejects_models_without_required_capabilities() -> None:
    with pytest.raises(ValueError, match="image_output"):
        plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.CHAT_STANDARD)
    with pytest.raises(ValueError, match="tools"):
        plan_execution(Feature.CHAT, GoogleModelKey.IMAGE)

    with pytest.raises(ValueError) as chat_error:
        plan_execution(Feature.CHAT, GoogleModelKey.TTS)
    for capability in ("text_output", "tools"):
        assert capability in str(chat_error.value)

    with pytest.raises(ValueError) as video_error:
        plan_execution(Feature.VIDEO_GENERATE, GoogleModelKey.TTS)
    assert "image_input" in str(video_error.value)
    assert "video_output" in str(video_error.value)


def test_plan_rejects_noncanonical_catalog_clones() -> None:
    model = get_google_model(GoogleModelKey.CHAT_STANDARD)
    clone = replace(model, provider_model_id="unpriced-model")

    with pytest.raises(ValueError, match="canonical catalog model"):
        plan_execution(Feature.CHAT, clone)


def test_semantic_roles_default_to_openrouter_with_google_as_explicit_rollback() -> (
    None
):
    default = plan_execution(Feature.CHAT, GoogleModelKey.CHAT_STANDARD)
    rollback = plan_execution(
        Feature.CHAT,
        GoogleModelKey.CHAT_STANDARD,
        provider=InferenceProvider.GOOGLE,
    )

    assert default.model is get_openrouter_model(GoogleModelKey.CHAT_STANDARD)
    assert rollback.model is get_google_model(GoogleModelKey.CHAT_STANDARD)


def test_enabled_features_expand_to_deterministic_model_roles() -> None:
    roles = model_roles_for_features(
        {Feature.CHAT, Feature.IMAGE_GENERATE, Feature.TRANSCRIBE}
    )

    assert roles == (
        GoogleModelKey.CHAT_ECONOMY,
        GoogleModelKey.CHAT_STANDARD,
        GoogleModelKey.CHAT_MULTIMODAL,
        GoogleModelKey.IMAGE,
        GoogleModelKey.STT,
        GoogleModelKey.FREE_TEXT,
        GoogleModelKey.FREE_VISUAL,
        GoogleModelKey.FREE_AUDIO,
    )


def test_enabled_feature_role_mapping_rejects_untyped_values() -> None:
    with pytest.raises(TypeError, match="Feature"):
        model_roles_for_features({"chat"})  # type: ignore[arg-type]


def test_plan_is_frozen_and_retains_exact_catalog_spec() -> None:
    model = get_google_model(GoogleModelKey.CHAT_STANDARD)
    plan = ExecutionPlan(feature=Feature.CHAT, model=model)

    assert plan.model is model
    with pytest.raises(FrozenInstanceError):
        plan.feature = Feature.INLINE_CHAT  # type: ignore[misc]


def test_execution_plan_scope_preserves_identity_and_resets() -> None:
    plan = plan_execution(Feature.TTS, GoogleModelKey.TTS)

    with pytest.raises(RuntimeError, match="no access-selected plan"):
        require_execution_plan(Feature.TTS)

    with execution_plan_scope(plan):
        assert require_execution_plan(Feature.TTS) is plan
        with pytest.raises(ValueError, match="cannot execute image_generate"):
            require_execution_plan(Feature.IMAGE_GENERATE)

    with pytest.raises(RuntimeError, match="no access-selected plan"):
        require_execution_plan(Feature.TTS)


def test_outcome_variants_make_payload_states_explicit() -> None:
    success = Succeeded(value="artifact")
    rejected = Rejected(reason=RejectionReason.POLICY)
    failed = Failed(reason=FailureReason.PROVIDER_ERROR)

    assert success.value == "artifact"
    assert not hasattr(rejected, "value")
    assert not hasattr(failed, "value")
    with pytest.raises(TypeError):
        Succeeded()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        Rejected(reason=RejectionReason.INVALID_INPUT, value="illegal")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        Failed(reason=FailureReason.PROVIDER_ERROR, value="illegal")  # type: ignore[call-arg]
    with pytest.raises(FrozenInstanceError):
        success.value = "changed"  # type: ignore[misc]
