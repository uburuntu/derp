"""Locked persistence commands for user and chat inference privacy policy."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from derp.inference.privacy import (
    ChatFreeModelPolicy,
    InferencePrivacyMode,
    InferencePrivacyPreference,
)
from derp.models import Chat, User

_PREFERENCE_COLUMNS = (
    User.inference_privacy_mode,
    User.inference_privacy_revision,
    User.free_inference_tos_version,
    User.free_inference_privacy_version,
    User.free_inference_accepted_at,
    User.free_inference_revoked_at,
)

_CHAT_POLICY_COLUMNS = (
    Chat.free_inference_enabled,
    Chat.free_inference_revision,
    Chat.free_inference_tos_version,
    Chat.free_inference_privacy_version,
    Chat.free_inference_accepted_by_user_id,
    Chat.free_inference_accepted_at,
    Chat.free_inference_revoked_at,
)


class InferencePrivacyRevisionConflictError(RuntimeError):
    """The rendered preference control no longer targets the current state."""

    def __init__(
        self,
        *,
        expected_revision: int,
        current: InferencePrivacyPreference,
    ) -> None:
        self.expected_revision = expected_revision
        self.current = current
        super().__init__("inference privacy preference revision changed")


class ChatFreeModelRevisionConflictError(RuntimeError):
    """The rendered chat policy control no longer targets current state."""

    def __init__(
        self,
        *,
        expected_revision: int,
        current: ChatFreeModelPolicy,
    ) -> None:
        self.expected_revision = expected_revision
        self.current = current
        super().__init__("chat free-model policy revision changed")


async def get_chat_free_model_policy(
    session: AsyncSession,
    chat_id: uuid.UUID,
) -> ChatFreeModelPolicy:
    """Return one content-free chat policy without loading chat metadata."""
    _require_uuid(chat_id, "chat_id")
    return await _load_chat_policy(session, chat_id=chat_id, for_update=False)


async def enable_chat_non_zdr_free_inference(
    session: AsyncSession,
    chat_id: uuid.UUID,
    *,
    accepted_by_user_id: uuid.UUID,
    expected_revision: int,
    tos_version: str,
    privacy_version: str,
    accepted_at: datetime | None = None,
) -> ChatFreeModelPolicy:
    """Atomically enable free fallback after a live administrator accepts."""
    _require_uuid(chat_id, "chat_id")
    _require_uuid(accepted_by_user_id, "accepted_by_user_id")
    _require_revision(expected_revision)
    timestamp = accepted_at or datetime.now(UTC)
    current = await _load_chat_policy(session, chat_id=chat_id, for_update=True)
    _require_current_chat_revision(current, expected_revision=expected_revision)
    enabled = current.enable(
        tos_version=tos_version,
        privacy_version=privacy_version,
        accepted_by_user_id=accepted_by_user_id,
        accepted_at=timestamp,
    )
    if enabled is current:
        return current
    await _store_chat_policy(session, chat_id=chat_id, policy=enabled)
    return enabled


async def revoke_chat_non_zdr_free_inference(
    session: AsyncSession,
    chat_id: uuid.UUID,
    *,
    expected_revision: int,
    revoked_at: datetime | None = None,
) -> ChatFreeModelPolicy:
    """Atomically disable future free fallback for one chat."""
    _require_uuid(chat_id, "chat_id")
    _require_revision(expected_revision)
    timestamp = revoked_at or datetime.now(UTC)
    current = await _load_chat_policy(session, chat_id=chat_id, for_update=True)
    _require_current_chat_revision(current, expected_revision=expected_revision)
    revoked = current.revoke(revoked_at=timestamp)
    if revoked is current:
        return current
    await _store_chat_policy(session, chat_id=chat_id, policy=revoked)
    return revoked


async def get_inference_privacy_preference(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> InferencePrivacyPreference:
    """Return one content-free preference snapshot without loading user PII."""
    _require_user_id(user_id)
    return await _load_preference(session, user_id=user_id, for_update=False)


async def accept_non_zdr_free_inference(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    expected_revision: int,
    tos_version: str,
    privacy_version: str,
    accepted_at: datetime | None = None,
) -> InferencePrivacyPreference:
    """Atomically record explicit current legal consent for non-ZDR free mode."""
    _require_user_id(user_id)
    _require_revision(expected_revision)
    timestamp = accepted_at or datetime.now(UTC)
    current = await _load_preference(session, user_id=user_id, for_update=True)
    _require_current_revision(current, expected_revision=expected_revision)
    accepted = current.accept_non_zdr_free(
        tos_version=tos_version,
        privacy_version=privacy_version,
        accepted_at=timestamp,
    )
    if accepted is current:
        return current
    await _store_preference(session, user_id=user_id, preference=accepted)
    return accepted


async def revoke_non_zdr_free_inference(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    expected_revision: int,
    revoked_at: datetime | None = None,
) -> InferencePrivacyPreference:
    """Atomically restore private-only mode while retaining acceptance audit."""
    _require_user_id(user_id)
    _require_revision(expected_revision)
    timestamp = revoked_at or datetime.now(UTC)
    current = await _load_preference(session, user_id=user_id, for_update=True)
    _require_current_revision(current, expected_revision=expected_revision)
    revoked = current.revoke_non_zdr_free(revoked_at=timestamp)
    if revoked is current:
        return current
    await _store_preference(session, user_id=user_id, preference=revoked)
    return revoked


async def _load_preference(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    for_update: bool,
) -> InferencePrivacyPreference:
    statement = select(*_PREFERENCE_COLUMNS).where(User.id == user_id)
    if for_update:
        statement = statement.with_for_update()
    result = await session.execute(statement)
    row = result.mappings().one_or_none()
    if row is None:
        raise LookupError("user inference privacy preference does not exist")
    return _preference_from_row(row)


async def _load_chat_policy(
    session: AsyncSession,
    *,
    chat_id: uuid.UUID,
    for_update: bool,
) -> ChatFreeModelPolicy:
    statement = select(*_CHAT_POLICY_COLUMNS).where(Chat.id == chat_id)
    if for_update:
        statement = statement.with_for_update()
    result = await session.execute(statement)
    row = result.mappings().one_or_none()
    if row is None:
        raise LookupError("chat free-model policy does not exist")
    return _chat_policy_from_row(row)


async def _store_preference(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    preference: InferencePrivacyPreference,
) -> None:
    await session.execute(
        update(User)
        .where(User.id == user_id)
        .values(
            inference_privacy_mode=preference.mode.value,
            inference_privacy_revision=preference.revision,
            free_inference_tos_version=preference.accepted_tos_version,
            free_inference_privacy_version=preference.accepted_privacy_version,
            free_inference_accepted_at=preference.accepted_at,
            free_inference_revoked_at=preference.revoked_at,
            updated_at=preference.revoked_at or preference.accepted_at,
        )
    )


async def _store_chat_policy(
    session: AsyncSession,
    *,
    chat_id: uuid.UUID,
    policy: ChatFreeModelPolicy,
) -> None:
    await session.execute(
        update(Chat)
        .where(Chat.id == chat_id)
        .values(
            free_inference_enabled=policy.enabled,
            free_inference_revision=policy.revision,
            free_inference_tos_version=policy.accepted_tos_version,
            free_inference_privacy_version=policy.accepted_privacy_version,
            free_inference_accepted_by_user_id=policy.accepted_by_user_id,
            free_inference_accepted_at=policy.accepted_at,
            free_inference_revoked_at=policy.revoked_at,
            updated_at=policy.revoked_at or policy.accepted_at,
        )
    )


def _preference_from_row(row: RowMapping) -> InferencePrivacyPreference:
    return InferencePrivacyPreference(
        mode=InferencePrivacyMode(row["inference_privacy_mode"]),
        revision=row["inference_privacy_revision"],
        accepted_tos_version=row["free_inference_tos_version"],
        accepted_privacy_version=row["free_inference_privacy_version"],
        accepted_at=row["free_inference_accepted_at"],
        revoked_at=row["free_inference_revoked_at"],
    )


def _chat_policy_from_row(row: RowMapping) -> ChatFreeModelPolicy:
    return ChatFreeModelPolicy(
        enabled=row["free_inference_enabled"],
        revision=row["free_inference_revision"],
        accepted_tos_version=row["free_inference_tos_version"],
        accepted_privacy_version=row["free_inference_privacy_version"],
        accepted_by_user_id=row["free_inference_accepted_by_user_id"],
        accepted_at=row["free_inference_accepted_at"],
        revoked_at=row["free_inference_revoked_at"],
    )


def _require_user_id(user_id: object) -> None:
    if not isinstance(user_id, uuid.UUID):
        raise TypeError("user_id must be a UUID")


def _require_uuid(value: object, name: str) -> None:
    if not isinstance(value, uuid.UUID):
        raise TypeError(f"{name} must be a UUID")


def _require_revision(revision: object) -> None:
    if isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
        raise ValueError("expected_revision must be a positive integer")


def _require_current_revision(
    current: InferencePrivacyPreference,
    *,
    expected_revision: int,
) -> None:
    if current.revision != expected_revision:
        raise InferencePrivacyRevisionConflictError(
            expected_revision=expected_revision,
            current=current,
        )


def _require_current_chat_revision(
    current: ChatFreeModelPolicy,
    *,
    expected_revision: int,
) -> None:
    if current.revision != expected_revision:
        raise ChatFreeModelRevisionConflictError(
            expected_revision=expected_revision,
            current=current,
        )


__all__ = [
    "ChatFreeModelRevisionConflictError",
    "InferencePrivacyRevisionConflictError",
    "accept_non_zdr_free_inference",
    "enable_chat_non_zdr_free_inference",
    "get_chat_free_model_policy",
    "get_inference_privacy_preference",
    "revoke_chat_non_zdr_free_inference",
    "revoke_non_zdr_free_inference",
]
