"""Structured chat policy mutations are typed, bounded, and serialized."""

import pytest

from derp.db.history import set_admin_policy, set_chat_policy_flag
from derp.history.policy import ChatPolicyFlag

pytestmark = pytest.mark.database


async def test_policy_flags_update_only_the_allowlisted_setting(
    db_session, chat_factory
) -> None:
    chat = await chat_factory(telegram_id=-9_200_001)

    await set_chat_policy_flag(
        db_session,
        chat_telegram_id=chat.telegram_id,
        flag=ChatPolicyFlag.EXPENSIVE_TOOLS,
        enabled=False,
    )
    await db_session.refresh(chat)

    assert chat.expensive_tools_enabled is False
    assert chat.shared_credit_spending_enabled is True
    assert chat.shared_facts_member_edit is False


async def test_admin_policy_is_trimmed_clearable_and_bounded(
    db_session, chat_factory
) -> None:
    chat = await chat_factory(telegram_id=-9_200_002)

    assert (
        await set_admin_policy(
            db_session,
            chat_telegram_id=chat.telegram_id,
            policy="  Prefer concise Python examples.  ",
        )
        == "Prefer concise Python examples."
    )
    assert (
        await set_admin_policy(
            db_session,
            chat_telegram_id=chat.telegram_id,
            policy="   ",
        )
        is None
    )
    with pytest.raises(ValueError, match="2048"):
        await set_admin_policy(
            db_session,
            chat_telegram_id=chat.telegram_id,
            policy="x" * 2049,
        )


async def test_policy_mutation_rejects_untyped_flags(db_session, chat_factory) -> None:
    chat = await chat_factory(telegram_id=-9_200_003)

    with pytest.raises(TypeError, match="ChatPolicyFlag"):
        await set_chat_policy_flag(
            db_session,
            chat_telegram_id=chat.telegram_id,
            flag="expensive_tools_enabled",  # type: ignore[arg-type]
            enabled=False,
        )
