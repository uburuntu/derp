"""Content-free user consent and eligibility for non-ZDR free inference."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, Protocol, Self

from derp.legal import PRIVACY_POLICY_URL, TERMS_OF_USE_URL

FREE_INFERENCE_LEGAL_VERSION_MAX_LENGTH: Final = 64
FREE_INFERENCE_TOS_URL: Final = TERMS_OF_USE_URL
FREE_INFERENCE_PRIVACY_URL: Final = PRIVACY_POLICY_URL
# Keep legal labels compact enough to share Telegram callback space with the
# preference revision that prevents stale consent controls from being replayed.
FREE_INFERENCE_TOS_VERSION: Final = "derp-terms-20260728"
FREE_INFERENCE_PRIVACY_VERSION: Final = "derp-privacy-20260728"
_LEGAL_VERSION = re.compile(
    rf"^[A-Za-z0-9._-]{{1,{FREE_INFERENCE_LEGAL_VERSION_MAX_LENGTH}}}$"
)


class InferencePrivacyMode(StrEnum):
    """A user's durable choice about provider data-retention terms."""

    PRIVATE_ONLY = "private_only"
    ALLOW_NON_ZDR_FREE = "allow_non_zdr_free"


class InferenceContext(StrEnum):
    """Content-free request contexts relevant to inference privacy."""

    PRIVATE = "private"
    INLINE = "inline"
    GROUP = "group"
    SUPERGROUP = "supergroup"
    CHANNEL = "channel"


class InferencePrivacyFields(Protocol):
    """Persisted preference fields needed at an inference boundary."""

    inference_privacy_mode: str
    inference_privacy_revision: int
    free_inference_tos_version: str | None
    free_inference_privacy_version: str | None
    free_inference_accepted_at: datetime | None
    free_inference_revoked_at: datetime | None


class NonZdrFreeInferenceReason(StrEnum):
    """Stable reasons for one non-ZDR free-inference decision."""

    ALLOWED = "allowed"
    CONTEXT_NOT_ALLOWED = "context_not_allowed"
    USER_OPT_IN_REQUIRED = "user_opt_in_required"
    LEGAL_REACCEPTANCE_REQUIRED = "legal_reacceptance_required"


@dataclass(frozen=True, slots=True)
class InferencePrivacyPreference:
    """Versioned current choice plus the last explicit legal acceptance."""

    mode: InferencePrivacyMode = InferencePrivacyMode.PRIVATE_ONLY
    revision: int = 1
    accepted_tos_version: str | None = None
    accepted_privacy_version: str | None = None
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, InferencePrivacyMode):
            raise TypeError("mode must be an InferencePrivacyMode")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision <= 0
        ):
            raise ValueError("revision must be a positive integer")

        acceptance = (
            self.accepted_tos_version,
            self.accepted_privacy_version,
            self.accepted_at,
        )
        if any(value is None for value in acceptance) and any(
            value is not None for value in acceptance
        ):
            raise ValueError("legal acceptance fields must be present together")
        if self.accepted_tos_version is not None:
            object.__setattr__(
                self,
                "accepted_tos_version",
                _legal_version(self.accepted_tos_version, "accepted_tos_version"),
            )
        if self.accepted_privacy_version is not None:
            object.__setattr__(
                self,
                "accepted_privacy_version",
                _legal_version(
                    self.accepted_privacy_version,
                    "accepted_privacy_version",
                ),
            )
        if self.accepted_at is not None:
            _require_aware(self.accepted_at, "accepted_at")
        if self.revoked_at is not None:
            _require_aware(self.revoked_at, "revoked_at")
            if self.accepted_at is None:
                raise ValueError("revocation requires a prior legal acceptance")
            if self.revoked_at < self.accepted_at:
                raise ValueError("revoked_at cannot precede accepted_at")

        if self.mode is InferencePrivacyMode.ALLOW_NON_ZDR_FREE:
            if self.accepted_at is None:
                raise ValueError("non-ZDR free mode requires legal acceptance")
            if self.revoked_at is not None:
                raise ValueError("active non-ZDR free mode cannot be revoked")
        elif self.accepted_at is None:
            if self.revoked_at is not None:
                raise ValueError("the default preference cannot be revoked")
        elif self.revoked_at is None:
            raise ValueError(
                "private-only mode must record accepted consent as revoked"
            )

    def accept_non_zdr_free(
        self,
        *,
        tos_version: str,
        privacy_version: str,
        accepted_at: datetime,
    ) -> Self:
        """Record an explicit legal acceptance or return an idempotent retry."""
        tos = _legal_version(tos_version, "tos_version")
        privacy = _legal_version(privacy_version, "privacy_version")
        _require_aware(accepted_at, "accepted_at")
        if (
            self.mode is InferencePrivacyMode.ALLOW_NON_ZDR_FREE
            and self.accepted_tos_version == tos
            and self.accepted_privacy_version == privacy
        ):
            return self
        last_change = self.revoked_at or self.accepted_at
        if last_change is not None and accepted_at < last_change:
            raise ValueError("accepted_at cannot precede the current preference state")
        return type(self)(
            mode=InferencePrivacyMode.ALLOW_NON_ZDR_FREE,
            revision=self.revision + 1,
            accepted_tos_version=tos,
            accepted_privacy_version=privacy,
            accepted_at=accepted_at,
        )

    def revoke_non_zdr_free(self, *, revoked_at: datetime) -> Self:
        """Return to private-only mode while retaining the last acceptance audit."""
        _require_aware(revoked_at, "revoked_at")
        if self.mode is InferencePrivacyMode.PRIVATE_ONLY:
            return self
        if self.accepted_at is None:  # pragma: no cover - guarded by construction
            raise RuntimeError("active free mode omitted its legal acceptance")
        if revoked_at < self.accepted_at:
            raise ValueError("revoked_at cannot precede accepted_at")
        return type(self)(
            mode=InferencePrivacyMode.PRIVATE_ONLY,
            revision=self.revision + 1,
            accepted_tos_version=self.accepted_tos_version,
            accepted_privacy_version=self.accepted_privacy_version,
            accepted_at=self.accepted_at,
            revoked_at=revoked_at,
        )


@dataclass(frozen=True, slots=True)
class NonZdrFreeInferenceDecision:
    """Identifier-free eligibility outcome for one request context."""

    reason: NonZdrFreeInferenceReason
    preference_revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.reason, NonZdrFreeInferenceReason):
            raise TypeError("reason must be a NonZdrFreeInferenceReason")
        if (
            isinstance(self.preference_revision, bool)
            or not isinstance(self.preference_revision, int)
            or self.preference_revision <= 0
        ):
            raise ValueError("preference_revision must be a positive integer")

    @property
    def allowed(self) -> bool:
        return self.reason is NonZdrFreeInferenceReason.ALLOWED


def decide_non_zdr_free_inference(
    preference: InferencePrivacyPreference,
    *,
    context: InferenceContext,
    current_tos_version: str,
    current_privacy_version: str,
) -> NonZdrFreeInferenceDecision:
    """Allow current legal consent only in private and inline contexts."""
    if not isinstance(preference, InferencePrivacyPreference):
        raise TypeError("preference must be an InferencePrivacyPreference")
    if not isinstance(context, InferenceContext):
        raise TypeError("context must be an InferenceContext")
    tos = _legal_version(current_tos_version, "current_tos_version")
    privacy = _legal_version(current_privacy_version, "current_privacy_version")

    if context not in {InferenceContext.PRIVATE, InferenceContext.INLINE}:
        reason = NonZdrFreeInferenceReason.CONTEXT_NOT_ALLOWED
    elif preference.mode is InferencePrivacyMode.PRIVATE_ONLY:
        reason = NonZdrFreeInferenceReason.USER_OPT_IN_REQUIRED
    elif (
        preference.accepted_tos_version != tos
        or preference.accepted_privacy_version != privacy
    ):
        reason = NonZdrFreeInferenceReason.LEGAL_REACCEPTANCE_REQUIRED
    else:
        reason = NonZdrFreeInferenceReason.ALLOWED
    return NonZdrFreeInferenceDecision(reason, preference.revision)


def project_inference_privacy(
    fields: InferencePrivacyFields,
) -> InferencePrivacyPreference:
    """Project persisted fields and fail closed on invalid legacy state."""
    try:
        return InferencePrivacyPreference(
            mode=InferencePrivacyMode(fields.inference_privacy_mode),
            revision=fields.inference_privacy_revision,
            accepted_tos_version=fields.free_inference_tos_version,
            accepted_privacy_version=fields.free_inference_privacy_version,
            accepted_at=fields.free_inference_accepted_at,
            revoked_at=fields.free_inference_revoked_at,
        )
    except TypeError, ValueError:
        return InferencePrivacyPreference()


def _legal_version(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    normalized = value.strip()
    if _LEGAL_VERSION.fullmatch(normalized) is None:
        raise ValueError(f"{name} must be a bounded ASCII version label")
    return normalized


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


__all__ = [
    "FREE_INFERENCE_LEGAL_VERSION_MAX_LENGTH",
    "FREE_INFERENCE_PRIVACY_URL",
    "FREE_INFERENCE_PRIVACY_VERSION",
    "FREE_INFERENCE_TOS_URL",
    "FREE_INFERENCE_TOS_VERSION",
    "InferenceContext",
    "InferencePrivacyFields",
    "InferencePrivacyMode",
    "InferencePrivacyPreference",
    "NonZdrFreeInferenceDecision",
    "NonZdrFreeInferenceReason",
    "decide_non_zdr_free_inference",
    "project_inference_privacy",
]
