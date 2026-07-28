"""Chat model choice uses metadata, funding role, and explicit privacy consent."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from derp.catalog import InferenceProvider, ModelRole
from derp.config import settings
from derp.execution import Feature
from derp.handlers.chat import _select_chat_plans
from derp.history.core import AttachmentReference, Speaker, UserTextTurn
from derp.inference import (
    FREE_INFERENCE_PRIVACY_VERSION,
    FREE_INFERENCE_TOS_VERSION,
    InferencePrivacyMode,
)
from derp.models import User

NOW = datetime(2026, 7, 21, 12, tzinfo=UTC)


def _user(*, free_mode: bool = False) -> User:
    return User(
        id=UUID(int=1),
        telegram_id=10,
        is_bot=False,
        first_name="User",
        is_premium=False,
        credits=0,
        inference_privacy_mode=(
            InferencePrivacyMode.ALLOW_NON_ZDR_FREE.value
            if free_mode
            else InferencePrivacyMode.PRIVATE_ONLY.value
        ),
        inference_privacy_revision=2 if free_mode else 1,
        free_inference_tos_version=(FREE_INFERENCE_TOS_VERSION if free_mode else None),
        free_inference_privacy_version=(
            FREE_INFERENCE_PRIVACY_VERSION if free_mode else None
        ),
        free_inference_accepted_at=NOW if free_mode else None,
        free_inference_revoked_at=None,
    )


def _turn(media_type: str | None = None) -> UserTextTurn:
    attachments = (
        (
            AttachmentReference(
                media_type=media_type,
                file_id="file-1",
                file_unique_id="unique-1",
            ),
        )
        if media_type
        else ()
    )
    return UserTextTurn(
        source_message_id=1,
        timestamp=NOW,
        speaker=Speaker(10, "User"),
        text="Question",
        attachments=attachments,
    )


@pytest.mark.parametrize("media_type", ["audio", "voice", "video", "video_note"])
def test_paid_audio_and_video_select_multimodal_before_download(
    media_type: str,
) -> None:
    paid, fallback = _select_chat_plans(
        user=_user(),
        chat_type="private",
        turn=_turn(media_type),
    )

    assert paid.model.key is ModelRole.CHAT_MULTIMODAL
    assert fallback is None


def test_paid_text_selects_sonnet_and_private_default_requires_payment() -> None:
    paid, fallback = _select_chat_plans(
        user=_user(),
        chat_type="private",
        turn=_turn(),
    )

    assert paid.model.key is ModelRole.CHAT_STANDARD
    assert paid.model.provider_model_id == "anthropic/claude-sonnet-5"
    assert fallback is None


def test_current_private_consent_selects_free_models_without_paid_fallback() -> None:
    _, text = _select_chat_plans(
        user=_user(free_mode=True),
        chat_type="private",
        turn=_turn(),
    )
    _, visual = _select_chat_plans(
        user=_user(free_mode=True),
        chat_type="private",
        turn=_turn("photo"),
    )
    _, audio = _select_chat_plans(
        user=_user(free_mode=True),
        chat_type="private",
        turn=_turn("voice"),
    )
    _, video = _select_chat_plans(
        user=_user(free_mode=True),
        chat_type="private",
        turn=_turn("video"),
    )

    assert text is not None and text.model.key is ModelRole.FREE_TEXT
    assert visual is not None and visual.model.key is ModelRole.FREE_VISUAL
    assert audio is not None and audio.model.key is ModelRole.FREE_AUDIO
    assert video is not None and video.model.key is ModelRole.FREE_VISUAL


def test_group_context_never_uses_members_non_zdr_preference() -> None:
    _, fallback = _select_chat_plans(
        user=_user(free_mode=True),
        chat_type="supergroup",
        turn=_turn(),
    )

    assert fallback is None


def test_empty_feature_set_is_explicit_direct_google_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "openrouter_enabled_features", frozenset())

    paid, fallback = _select_chat_plans(
        user=_user(free_mode=True),
        chat_type="private",
        turn=_turn("voice"),
    )

    assert paid.model.provider is InferenceProvider.GOOGLE
    assert paid.model.key is ModelRole.CHAT_STANDARD
    assert fallback is None
    assert Feature.CHAT not in settings.openrouter_enabled_features
