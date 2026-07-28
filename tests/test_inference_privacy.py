"""User choice and persistence contracts for non-ZDR free inference."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update

from derp.db.inference_privacy import (
    InferencePrivacyRevisionConflictError,
    accept_non_zdr_free_inference,
    get_inference_privacy_preference,
    revoke_non_zdr_free_inference,
)
from derp.inference import (
    InferenceContext,
    InferencePrivacyMode,
    InferencePrivacyPreference,
    NonZdrFreeInferenceReason,
    decide_non_zdr_free_inference,
)
from derp.models import User

USER_ID = UUID(int=1)
ACCEPTED_AT = datetime(2026, 7, 21, 12, tzinfo=UTC)
REVOKED_AT = ACCEPTED_AT + timedelta(hours=1)
TOS_VERSION = "tos-2026-07-21"
PRIVACY_VERSION = "privacy-2026-07-21"


def _accepted_preference() -> InferencePrivacyPreference:
    return InferencePrivacyPreference().accept_non_zdr_free(
        tos_version=TOS_VERSION,
        privacy_version=PRIVACY_VERSION,
        accepted_at=ACCEPTED_AT,
    )


def _row(preference: InferencePrivacyPreference) -> dict[str, object]:
    return {
        "inference_privacy_mode": preference.mode.value,
        "inference_privacy_revision": preference.revision,
        "free_inference_tos_version": preference.accepted_tos_version,
        "free_inference_privacy_version": preference.accepted_privacy_version,
        "free_inference_accepted_at": preference.accepted_at,
        "free_inference_revoked_at": preference.revoked_at,
    }


def _query_result(row: dict[str, object] | None) -> MagicMock:
    result = MagicMock()
    result.mappings.return_value.one_or_none.return_value = row
    return result


def _session(*results: MagicMock) -> MagicMock:
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock(side_effect=results)
    return session


def test_default_preference_is_private_only_without_implicit_acceptance() -> None:
    preference = InferencePrivacyPreference()

    assert preference.mode is InferencePrivacyMode.PRIVATE_ONLY
    assert preference.revision == 1
    assert preference.accepted_tos_version is None
    assert preference.accepted_privacy_version is None
    assert preference.accepted_at is None
    assert preference.revoked_at is None

    for context in (InferenceContext.PRIVATE, InferenceContext.INLINE):
        decision = decide_non_zdr_free_inference(
            preference,
            context=context,
            current_tos_version=TOS_VERSION,
            current_privacy_version=PRIVACY_VERSION,
        )
        assert not decision.allowed
        assert decision.reason is NonZdrFreeInferenceReason.USER_OPT_IN_REQUIRED


@pytest.mark.parametrize(
    "context",
    [InferenceContext.PRIVATE, InferenceContext.INLINE],
)
def test_current_explicit_acceptance_allows_only_supported_personal_contexts(
    context: InferenceContext,
) -> None:
    decision = decide_non_zdr_free_inference(
        _accepted_preference(),
        context=context,
        current_tos_version=TOS_VERSION,
        current_privacy_version=PRIVACY_VERSION,
    )

    assert decision.allowed
    assert decision.reason is NonZdrFreeInferenceReason.ALLOWED
    assert decision.preference_revision == 2


@pytest.mark.parametrize(
    "context",
    [InferenceContext.GROUP, InferenceContext.SUPERGROUP, InferenceContext.CHANNEL],
)
def test_non_zdr_free_inference_is_never_eligible_outside_private_or_inline(
    context: InferenceContext,
) -> None:
    decision = decide_non_zdr_free_inference(
        _accepted_preference(),
        context=context,
        current_tos_version=TOS_VERSION,
        current_privacy_version=PRIVACY_VERSION,
    )

    assert not decision.allowed
    assert decision.reason is NonZdrFreeInferenceReason.CONTEXT_NOT_ALLOWED


@pytest.mark.parametrize(
    ("tos_version", "privacy_version"),
    [
        ("tos-next", PRIVACY_VERSION),
        (TOS_VERSION, "privacy-next"),
    ],
)
def test_any_legal_version_change_requires_fresh_acceptance(
    tos_version: str,
    privacy_version: str,
) -> None:
    decision = decide_non_zdr_free_inference(
        _accepted_preference(),
        context=InferenceContext.PRIVATE,
        current_tos_version=tos_version,
        current_privacy_version=privacy_version,
    )

    assert not decision.allowed
    assert decision.reason is NonZdrFreeInferenceReason.LEGAL_REACCEPTANCE_REQUIRED


def test_preference_can_be_revoked_and_explicitly_accepted_again() -> None:
    accepted = _accepted_preference()
    revoked = accepted.revoke_non_zdr_free(revoked_at=REVOKED_AT)
    reaccepted = revoked.accept_non_zdr_free(
        tos_version=TOS_VERSION,
        privacy_version=PRIVACY_VERSION,
        accepted_at=REVOKED_AT + timedelta(minutes=1),
    )

    assert revoked.mode is InferencePrivacyMode.PRIVATE_ONLY
    assert revoked.revision == 3
    assert revoked.accepted_at == ACCEPTED_AT
    assert revoked.revoked_at == REVOKED_AT
    assert reaccepted.mode is InferencePrivacyMode.ALLOW_NON_ZDR_FREE
    assert reaccepted.revision == 4
    assert reaccepted.accepted_at == REVOKED_AT + timedelta(minutes=1)
    assert reaccepted.revoked_at is None


def test_accept_and_revoke_retries_are_idempotent() -> None:
    accepted = _accepted_preference()
    assert (
        accepted.accept_non_zdr_free(
            tos_version=TOS_VERSION,
            privacy_version=PRIVACY_VERSION,
            accepted_at=ACCEPTED_AT + timedelta(days=1),
        )
        is accepted
    )
    default = InferencePrivacyPreference()
    assert default.revoke_non_zdr_free(revoked_at=REVOKED_AT) is default


@pytest.mark.parametrize(
    "preference",
    [
        lambda: InferencePrivacyPreference(
            accepted_tos_version=TOS_VERSION,
        ),
        lambda: InferencePrivacyPreference(
            mode=InferencePrivacyMode.ALLOW_NON_ZDR_FREE,
        ),
        lambda: InferencePrivacyPreference(
            mode=InferencePrivacyMode.PRIVATE_ONLY,
            accepted_tos_version=TOS_VERSION,
            accepted_privacy_version=PRIVACY_VERSION,
            accepted_at=ACCEPTED_AT,
        ),
        lambda: InferencePrivacyPreference(
            mode=InferencePrivacyMode.ALLOW_NON_ZDR_FREE,
            accepted_tos_version=TOS_VERSION,
            accepted_privacy_version=PRIVACY_VERSION,
            accepted_at=ACCEPTED_AT,
            revoked_at=REVOKED_AT,
        ),
    ],
)
def test_incomplete_or_ambiguous_preference_states_are_rejected(preference) -> None:
    with pytest.raises(ValueError):
        preference()


def test_legal_acceptance_requires_aware_monotonic_timestamps() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        InferencePrivacyPreference().accept_non_zdr_free(
            tos_version=TOS_VERSION,
            privacy_version=PRIVACY_VERSION,
            accepted_at=datetime(2026, 7, 21),
        )
    with pytest.raises(ValueError, match="cannot precede"):
        _accepted_preference().revoke_non_zdr_free(
            revoked_at=ACCEPTED_AT - timedelta(seconds=1)
        )


@pytest.mark.parametrize("version", ["", " ", "tos/v2", "x" * 65])
def test_legal_versions_are_bounded_safe_labels(version: str) -> None:
    with pytest.raises(ValueError, match="bounded ASCII"):
        InferencePrivacyPreference().accept_non_zdr_free(
            tos_version=version,
            privacy_version=PRIVACY_VERSION,
            accepted_at=ACCEPTED_AT,
        )


def test_user_schema_has_private_only_server_defaults_and_state_constraints() -> None:
    table = User.__table__
    assert table.c.inference_privacy_mode.server_default is not None
    assert table.c.inference_privacy_mode.server_default.arg.text == "'private_only'"
    assert table.c.inference_privacy_revision.server_default is not None
    assert table.c.inference_privacy_revision.server_default.arg.text == "1"
    constraint_names = {constraint.name for constraint in table.constraints}
    assert {
        "user_inference_privacy_mode_allowed",
        "user_inference_privacy_revision_positive",
        "user_free_inference_acceptance_complete",
        "user_free_inference_versions_nonblank",
        "user_free_inference_state_complete",
        "user_free_inference_revocation_order",
    } <= constraint_names


async def test_preference_query_selects_only_content_free_columns() -> None:
    preference = _accepted_preference()
    session = _session(_query_result(_row(preference)))

    loaded = await get_inference_privacy_preference(session, USER_ID)

    assert loaded == preference
    statement = session.execute.await_args.args[0]
    sql = str(statement)
    assert "inference_privacy_mode" in sql
    assert "telegram_id" not in sql
    assert "first_name" not in sql


async def test_accept_query_locks_then_persists_complete_legal_choice() -> None:
    session = _session(_query_result(_row(InferencePrivacyPreference())), MagicMock())

    preference = await accept_non_zdr_free_inference(
        session,
        USER_ID,
        expected_revision=1,
        tos_version=TOS_VERSION,
        privacy_version=PRIVACY_VERSION,
        accepted_at=ACCEPTED_AT,
    )

    assert preference == _accepted_preference()
    select_statement = session.execute.await_args_list[0].args[0]
    update_statement = session.execute.await_args_list[1].args[0]
    assert select_statement._for_update_arg is not None
    assert isinstance(update_statement, Update)
    params = update_statement.compile().params
    assert params["inference_privacy_mode"] == "allow_non_zdr_free"
    assert params["inference_privacy_revision"] == 2
    assert params["free_inference_tos_version"] == TOS_VERSION
    assert params["free_inference_privacy_version"] == PRIVACY_VERSION
    assert params["free_inference_accepted_at"] == ACCEPTED_AT
    assert params["free_inference_revoked_at"] is None


async def test_revoke_query_preserves_acceptance_and_records_reversal() -> None:
    accepted = _accepted_preference()
    session = _session(_query_result(_row(accepted)), MagicMock())

    preference = await revoke_non_zdr_free_inference(
        session,
        USER_ID,
        expected_revision=accepted.revision,
        revoked_at=REVOKED_AT,
    )

    assert preference.mode is InferencePrivacyMode.PRIVATE_ONLY
    assert preference.revision == 3
    assert preference.accepted_tos_version == TOS_VERSION
    assert preference.accepted_privacy_version == PRIVACY_VERSION
    assert preference.accepted_at == ACCEPTED_AT
    assert preference.revoked_at == REVOKED_AT
    params = session.execute.await_args_list[1].args[0].compile().params
    assert params["inference_privacy_mode"] == "private_only"
    assert params["free_inference_revoked_at"] == REVOKED_AT


async def test_idempotent_query_retry_does_not_write_or_advance_revision() -> None:
    accepted = _accepted_preference()
    session = _session(_query_result(_row(accepted)))

    result = await accept_non_zdr_free_inference(
        session,
        USER_ID,
        expected_revision=accepted.revision,
        tos_version=TOS_VERSION,
        privacy_version=PRIVACY_VERSION,
        accepted_at=ACCEPTED_AT + timedelta(days=1),
    )

    assert result is not accepted
    assert result == accepted
    session.execute.assert_awaited_once()


@pytest.mark.parametrize("action", ["accept", "revoke"])
async def test_stale_mutation_revision_is_rejected_under_lock(action: str) -> None:
    accepted = _accepted_preference()
    revoked = accepted.revoke_non_zdr_free(revoked_at=REVOKED_AT)
    session = _session(_query_result(_row(revoked)))

    with pytest.raises(InferencePrivacyRevisionConflictError) as raised:
        if action == "accept":
            await accept_non_zdr_free_inference(
                session,
                USER_ID,
                expected_revision=1,
                tos_version=TOS_VERSION,
                privacy_version=PRIVACY_VERSION,
            )
        else:
            await revoke_non_zdr_free_inference(
                session,
                USER_ID,
                expected_revision=accepted.revision,
            )

    assert raised.value.expected_revision in {1, accepted.revision}
    assert raised.value.current == revoked
    statement = session.execute.await_args.args[0]
    assert statement._for_update_arg is not None
    session.execute.assert_awaited_once()


async def test_preference_query_fails_closed_for_unknown_user() -> None:
    session = _session(_query_result(None))

    with pytest.raises(LookupError, match="does not exist"):
        await get_inference_privacy_preference(session, USER_ID)
