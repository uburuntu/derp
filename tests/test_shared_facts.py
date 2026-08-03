"""PostgreSQL contracts for topic-scoped shared factual memory."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from derp.db.history import clear_history_scope
from derp.db.queries import upsert_message
from derp.db.shared_facts import (
    SharedFactDecisionConflictError,
    approve_shared_fact,
    forget_approved_shared_facts,
    list_approved_shared_facts,
    propose_shared_fact,
    reject_shared_fact,
)
from derp.models import Message, SharedFact, SharedFactState

pytestmark = pytest.mark.database


async def test_proposal_and_review_preserve_actor_audit(
    db_session, chat_factory, user_factory
) -> None:
    chat = await chat_factory(telegram_id=-9_100_001)
    proposer = await user_factory(telegram_id=9_100_001)
    admin = await user_factory(telegram_id=9_100_002)
    reviewed_at = datetime(2026, 7, 20, 12, tzinfo=UTC)

    proposal = await propose_shared_fact(
        db_session,
        chat_id=chat.id,
        thread_id=42,
        proposer_user_id=proposer.id,
        fact_text="  The release window is Tuesday.  ",
    )

    assert proposal.fact_text == "The release window is Tuesday."
    assert proposal.state == SharedFactState.PROPOSED
    assert proposal.proposed_by_user_id == proposer.id
    assert proposal.decided_by_user_id is None
    assert proposal.decided_at is None

    reviewed = await approve_shared_fact(
        db_session,
        fact_id=proposal.id,
        chat_id=chat.id,
        thread_id=42,
        admin_actor_id=admin.id,
        decided_at=reviewed_at,
    )

    assert reviewed.state == SharedFactState.APPROVED
    assert reviewed.decided_by_user_id == admin.id
    assert reviewed.decided_at == reviewed_at
    assert reviewed.is_approved


async def test_approval_retry_preserves_first_admin_and_timestamp(
    db_session, chat_factory, user_factory
) -> None:
    chat = await chat_factory(telegram_id=-9_100_002)
    proposer = await user_factory(telegram_id=9_100_003)
    first_admin = await user_factory(telegram_id=9_100_004)
    retrying_admin = await user_factory(telegram_id=9_100_005)
    first_review = datetime(2026, 7, 20, 12, tzinfo=UTC)
    retry = first_review + timedelta(hours=1)
    proposal = await propose_shared_fact(
        db_session,
        chat_id=chat.id,
        thread_id=None,
        proposer_user_id=proposer.id,
        fact_text="The office is closed on Fridays.",
    )

    first = await approve_shared_fact(
        db_session,
        fact_id=proposal.id,
        chat_id=chat.id,
        thread_id=None,
        admin_actor_id=first_admin.id,
        decided_at=first_review,
    )
    repeated = await approve_shared_fact(
        db_session,
        fact_id=proposal.id,
        chat_id=chat.id,
        thread_id=None,
        admin_actor_id=retrying_admin.id,
        decided_at=retry,
    )

    assert repeated.id == first.id
    assert repeated.decided_by_user_id == first_admin.id
    assert repeated.decided_at == first_review


async def test_rejection_is_a_terminal_audited_decision(
    db_session, chat_factory, user_factory
) -> None:
    chat = await chat_factory(telegram_id=-9_100_003)
    proposer = await user_factory(telegram_id=9_100_006)
    admin = await user_factory(telegram_id=9_100_007)
    reviewed_at = datetime(2026, 7, 20, 14, tzinfo=UTC)
    proposal = await propose_shared_fact(
        db_session,
        chat_id=chat.id,
        thread_id=7,
        proposer_user_id=proposer.id,
        fact_text="The unverified claim is true.",
    )

    rejected = await reject_shared_fact(
        db_session,
        fact_id=proposal.id,
        chat_id=chat.id,
        thread_id=7,
        admin_actor_id=admin.id,
        decided_at=reviewed_at,
    )

    assert rejected.state == SharedFactState.REJECTED
    assert rejected.decided_by_user_id == admin.id
    assert rejected.decided_at == reviewed_at
    with pytest.raises(SharedFactDecisionConflictError, match="already rejected"):
        await approve_shared_fact(
            db_session,
            fact_id=proposal.id,
            chat_id=chat.id,
            thread_id=7,
            admin_actor_id=admin.id,
        )


async def test_approved_query_is_exactly_scoped_and_deterministic(
    db_session, chat_factory, user_factory
) -> None:
    chat = await chat_factory(telegram_id=-9_100_004)
    other_chat = await chat_factory(telegram_id=-9_100_005)
    proposer = await user_factory(telegram_id=9_100_008)
    admin = await user_factory(telegram_id=9_100_009)
    common_time = datetime(2026, 7, 20, 10, tzinfo=UTC)

    async def proposal_for(chat_id, thread_id, text):
        fact = await propose_shared_fact(
            db_session,
            chat_id=chat_id,
            thread_id=thread_id,
            proposer_user_id=proposer.id,
            fact_text=text,
        )
        fact.created_at = common_time
        return fact

    root_facts = [
        await proposal_for(chat.id, None, "Root fact A"),
        await proposal_for(chat.id, None, "Root fact B"),
    ]
    topic_fact = await proposal_for(chat.id, 99, "Topic-only fact")
    other_chat_fact = await proposal_for(other_chat.id, None, "Other-chat fact")
    rejected = await proposal_for(chat.id, None, "Rejected root fact")
    pending = await proposal_for(chat.id, None, "Pending root fact")
    await db_session.flush()

    for fact in [*root_facts, topic_fact, other_chat_fact]:
        await approve_shared_fact(
            db_session,
            fact_id=fact.id,
            chat_id=fact.chat_id,
            thread_id=fact.thread_id,
            admin_actor_id=admin.id,
        )
    await reject_shared_fact(
        db_session,
        fact_id=rejected.id,
        chat_id=chat.id,
        thread_id=None,
        admin_actor_id=admin.id,
    )

    approved = await list_approved_shared_facts(
        db_session,
        chat_id=chat.id,
        thread_id=None,
    )

    expected = sorted(root_facts, key=lambda fact: (fact.created_at, fact.id))
    assert [fact.id for fact in approved] == [fact.id for fact in expected]
    assert pending.state == SharedFactState.PROPOSED


async def test_wrong_topic_cannot_review_a_proposal(
    db_session, chat_factory, user_factory
) -> None:
    chat = await chat_factory(telegram_id=-9_100_006)
    proposer = await user_factory(telegram_id=9_100_010)
    admin = await user_factory(telegram_id=9_100_011)
    proposal = await propose_shared_fact(
        db_session,
        chat_id=chat.id,
        thread_id=None,
        proposer_user_id=proposer.id,
        fact_text="Root scope only.",
    )

    with pytest.raises(LookupError, match="chat/topic scope"):
        await approve_shared_fact(
            db_session,
            fact_id=proposal.id,
            chat_id=chat.id,
            thread_id=1,
            admin_actor_id=admin.id,
        )

    await db_session.refresh(proposal)
    assert proposal.state == SharedFactState.PROPOSED


@pytest.mark.parametrize("fact_text", ["   ", "x" * 1025])
async def test_database_rejects_invalid_fact_text(
    db_session, chat_factory, user_factory, fact_text
) -> None:
    chat = await chat_factory(telegram_id=-9_100_007)
    proposer = await user_factory(telegram_id=9_100_012)
    db_session.add(
        SharedFact(
            chat_id=chat.id,
            thread_id=None,
            proposed_by_user_id=proposer.id,
            fact_text=fact_text,
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_database_rejects_incomplete_decision_audit(
    db_session, chat_factory, user_factory
) -> None:
    chat = await chat_factory(telegram_id=-9_100_008)
    proposer = await user_factory(telegram_id=9_100_013)
    db_session.add(
        SharedFact(
            chat_id=chat.id,
            thread_id=None,
            proposed_by_user_id=proposer.id,
            fact_text="Invalid approved row.",
            state=SharedFactState.APPROVED.value,
        )
    )

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_history_and_approved_facts_are_forgotten_independently(
    db_session, chat_factory, user_factory
) -> None:
    chat = await chat_factory(telegram_id=-9_100_009)
    proposer = await user_factory(telegram_id=9_100_014)
    admin = await user_factory(telegram_id=9_100_015)
    now = datetime.now(UTC)
    fact = await propose_shared_fact(
        db_session,
        chat_id=chat.id,
        thread_id=33,
        proposer_user_id=proposer.id,
        fact_text="The topic has a durable approved fact.",
    )
    await approve_shared_fact(
        db_session,
        fact_id=fact.id,
        chat_id=chat.id,
        thread_id=33,
        admin_actor_id=admin.id,
    )
    await upsert_message(
        db_session,
        chat_telegram_id=chat.telegram_id,
        user_telegram_id=proposer.telegram_id,
        telegram_message_id=1,
        thread_id=33,
        direction="in",
        content_type="text",
        text="Conversation content",
        telegram_date=now,
    )

    assert (
        await clear_history_scope(
            db_session,
            chat_telegram_id=chat.telegram_id,
            thread_id=33,
        )
        == 1
    )
    assert [
        stored.id
        for stored in await list_approved_shared_facts(
            db_session, chat_id=chat.id, thread_id=33
        )
    ] == [fact.id]

    await upsert_message(
        db_session,
        chat_telegram_id=chat.telegram_id,
        user_telegram_id=proposer.telegram_id,
        telegram_message_id=2,
        thread_id=33,
        direction="in",
        content_type="text",
        text="New conversation content",
        telegram_date=now,
    )
    assert (
        await forget_approved_shared_facts(
            db_session,
            chat_id=chat.id,
            thread_id=33,
        )
        == 1
    )
    assert not await list_approved_shared_facts(
        db_session, chat_id=chat.id, thread_id=33
    )
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Message).where(Message.chat_id == chat.id)
        )
        == 1
    )


async def test_forum_root_forget_covers_approved_facts_in_every_topic(
    db_session, chat_factory, user_factory
) -> None:
    chat = await chat_factory(telegram_id=-9_100_010)
    proposer = await user_factory(telegram_id=9_100_016)
    admin = await user_factory(telegram_id=9_100_017)

    async def approved_fact(thread_id: int | None, text: str) -> SharedFact:
        fact = await propose_shared_fact(
            db_session,
            chat_id=chat.id,
            thread_id=thread_id,
            proposer_user_id=proposer.id,
            fact_text=text,
        )
        return await approve_shared_fact(
            db_session,
            fact_id=fact.id,
            chat_id=chat.id,
            thread_id=thread_id,
            admin_actor_id=admin.id,
        )

    root = await approved_fact(None, "Root fact")
    await approved_fact(33, "Selected topic fact")
    sibling_topic = await approved_fact(44, "Sibling topic fact")

    assert (
        await forget_approved_shared_facts(
            db_session,
            chat_id=chat.id,
            thread_id=33,
        )
        == 1
    )
    assert [
        fact.id
        for fact in await list_approved_shared_facts(
            db_session,
            chat_id=chat.id,
            thread_id=None,
        )
    ] == [root.id]
    assert [
        fact.id
        for fact in await list_approved_shared_facts(
            db_session,
            chat_id=chat.id,
            thread_id=44,
        )
    ] == [sibling_topic.id]
    assert not await list_approved_shared_facts(
        db_session,
        chat_id=chat.id,
        thread_id=33,
    )

    assert (
        await forget_approved_shared_facts(
            db_session,
            chat_id=chat.id,
            thread_id=None,
            all_threads=True,
        )
        == 2
    )
    assert not await db_session.scalar(
        select(func.count())
        .select_from(SharedFact)
        .where(
            SharedFact.chat_id == chat.id,
            SharedFact.state == SharedFactState.APPROVED.value,
        )
    )
