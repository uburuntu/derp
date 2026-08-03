"""Long-form Telegram conversations through polling, HTTP, and PostgreSQL."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from derp.models import (
    Chat as ChatModel,
)
from derp.models import (
    InferenceUsage,
    PaymentUpdateInbox,
    SupportRequest,
)
from derp.models import (
    Message as MessageModel,
)
from tests.e2e.harness import (
    BlockingPaymentInbox,
    DeterministicChatModel,
    TelegramChat,
    TelegramConversation,
    TelegramUser,
    model_text,
)

pytestmark = [pytest.mark.database, pytest.mark.telegram_e2e]


async def test_private_conversation_survives_transport_retry_and_restart(
    telegram_conversation: TelegramConversation,
) -> None:
    actor = TelegramUser(
        id=7_701_001,
        first_name="Ada",
        last_name="Lovelace",
        username="ada",
    )
    chat = TelegramChat(
        id=actor.id,
        type="private",
        first_name=actor.first_name,
    )
    await telegram_conversation.seed_paid_scope(actor=actor, chat=chat)

    before_restart = DeterministicChatModel.from_responses(
        "Atlas noted.",
        "The launch target is Friday.",
        "The owner is Mira.",
        "The risk is database capacity.",
        "Summary saved in context.",
    )
    await telegram_conversation.start(before_restart)

    prompts = (
        "My project is Atlas.",
        "The launch target is Friday.",
        "Mira owns the rollout.",
        "The main risk is database capacity.",
        "Summarize what you know so far.",
    )
    expected_replies = (
        "Atlas noted.",
        "The launch target is Friday.",
        "The owner is Mira.",
        "The risk is database capacity.",
        "Summary saved in context.",
    )
    visible_replies: list[str] = []
    retry_cursor = 0
    for index, prompt in enumerate(prompts):
        if index == 3:
            retry_cursor = len(telegram_conversation.server.calls)
            telegram_conversation.server.fail_next(
                "sendMessage",
                error_code=400,
                description="Bad Request: can't parse entities: malformed fixture",
            )
        incoming = await telegram_conversation.send_text(
            actor=actor,
            chat=chat,
            text=prompt,
        )
        reply = await telegram_conversation.expect_reply(
            incoming,
            text=expected_replies[index],
            wait_persisted=True,
        )
        visible_replies.append(str(reply["text"]))

    assert visible_replies == list(expected_replies)
    assert len(before_restart.calls) == len(prompts)
    before_restart.assert_exhausted()
    second_context = model_text(before_restart.calls[1].messages)
    assert "My project is Atlas." in second_context
    assert "Atlas noted." in second_context

    retry_calls = [
        call
        for call in telegram_conversation.server.calls
        if call.sequence > retry_cursor and call.method == "sendMessage"
    ][:2]
    assert len(retry_calls) == 2
    assert retry_calls[0].fields["parse_mode"] == "HTML"
    assert "parse_mode" not in retry_calls[1].fields
    assert retry_calls[0].fields["text"] == retry_calls[1].fields["text"]

    after_restart = DeterministicChatModel.from_responses(
        "Atlas launches Friday; Mira owns it; database capacity is the risk."
    )
    await telegram_conversation.restart(after_restart)
    resumed = await telegram_conversation.send_text(
        actor=actor,
        chat=chat,
        text="After the restart, recap Atlas.",
    )
    resumed_reply = await telegram_conversation.expect_reply(
        resumed,
        text="Atlas launches Friday; Mira owns it; database capacity is the risk.",
        wait_persisted=True,
    )

    assert resumed_reply["text"].startswith("Atlas launches Friday")
    assert len(after_restart.calls) == 1
    after_restart.assert_exhausted()
    resumed_context = model_text(after_restart.calls[0].messages)
    for expected in (*prompts, *expected_replies, "After the restart, recap Atlas."):
        assert expected in resumed_context

    async with telegram_conversation.database.read_session() as session:
        messages = list(
            await session.scalars(
                select(MessageModel)
                .join(ChatModel, MessageModel.chat_id == ChatModel.id)
                .where(ChatModel.telegram_id == chat.id)
                .order_by(MessageModel.telegram_message_id)
            )
        )
        inference_count = await session.scalar(
            select(func.count()).select_from(InferenceUsage)
        )
    assert [message.direction for message in messages].count("in") == 6
    assert [message.direction for message in messages].count("out") == 6
    assert all(
        message.reply_to_message_id is not None
        for message in messages
        if message.direction == "out"
    )
    assert inference_count == 6


async def test_forum_topics_keep_ambient_and_assistant_history_isolated(
    telegram_conversation: TelegramConversation,
) -> None:
    actor = TelegramUser(
        id=7_702_001,
        first_name="Grace",
        last_name="Hopper",
        username="grace",
    )
    forum = TelegramChat(
        id=-100_770_200,
        type="supergroup",
        title="Release engineering",
        is_forum=True,
    )
    await telegram_conversation.seed_paid_scope(
        actor=actor,
        chat=forum,
        ambient_history_enabled=True,
        member_status="administrator",
    )
    model = DeterministicChatModel.from_responses(
        "Topic 11 deploys the dashboard on Tuesday.",
        "Topic 22 rotates the signing key on Thursday.",
    )
    await telegram_conversation.start(model)

    await asyncio.gather(
        telegram_conversation.send_text(
            actor=actor,
            chat=forum,
            thread_id=11,
            text="The dashboard deploy is Tuesday.",
        ),
        telegram_conversation.send_text(
            actor=actor,
            chat=forum,
            thread_id=22,
            text="The signing key rotation is Thursday.",
        ),
    )
    topic_11_question = await telegram_conversation.send_text(
        actor=actor,
        chat=forum,
        thread_id=11,
        text="Derp, what is this topic's schedule?",
    )
    topic_11_reply = await telegram_conversation.expect_reply(
        topic_11_question,
        text="Topic 11 deploys the dashboard on Tuesday.",
        wait_persisted=True,
    )
    topic_22_question = await telegram_conversation.send_text(
        actor=actor,
        chat=forum,
        thread_id=22,
        text="Derp, what is this topic's schedule?",
    )
    topic_22_reply = await telegram_conversation.expect_reply(
        topic_22_question,
        text="Topic 22 rotates the signing key on Thursday.",
        wait_persisted=True,
    )

    assert topic_11_reply["message_thread_id"] == 11
    assert topic_22_reply["message_thread_id"] == 22
    assert len(model.calls) == 2
    model.assert_exhausted()
    topic_11_context = model_text(model.calls[0].messages)
    topic_22_context = model_text(model.calls[1].messages)
    assert "dashboard deploy is Tuesday" in topic_11_context
    assert "signing key rotation is Thursday" not in topic_11_context
    assert "signing key rotation is Thursday" in topic_22_context
    assert "dashboard deploy is Tuesday" not in topic_22_context
    assert "Topic 11 deploys the dashboard" not in topic_22_context

    membership_calls = [
        call
        for call in telegram_conversation.server.calls
        if call.method == "getChatMember"
        and int(call.fields["chat_id"]) == forum.id
        and int(call.fields["user_id"]) == actor.id
    ]
    assert membership_calls

    async with telegram_conversation.database.read_session() as session:
        stored = list(
            await session.scalars(
                select(MessageModel)
                .join(ChatModel, MessageModel.chat_id == ChatModel.id)
                .where(ChatModel.telegram_id == forum.id)
                .order_by(
                    MessageModel.thread_id,
                    MessageModel.telegram_message_id,
                )
            )
        )
    assert [(message.thread_id, message.direction) for message in stored] == [
        (11, "in"),
        (11, "in"),
        (11, "out"),
        (22, "in"),
        (22, "in"),
        (22, "out"),
    ]


async def test_settings_buttons_round_trip_through_rendered_callback_data(
    telegram_conversation: TelegramConversation,
) -> None:
    actor = TelegramUser(id=7_703_001, first_name="Lin", username="lin")
    chat = TelegramChat(id=actor.id, type="private", first_name=actor.first_name)
    await telegram_conversation.seed_paid_scope(actor=actor, chat=chat)
    model = DeterministicChatModel.expecting_no_calls()
    await telegram_conversation.start(model)

    command = await telegram_conversation.send_text(
        actor=actor,
        chat=chat,
        text="/settings",
        wait_persisted=False,
    )
    panel = await telegram_conversation.expect_reply(command)
    assert str(panel["text"]).startswith("Derp\n")
    settings_call = next(
        call
        for call in telegram_conversation.server.calls
        if call.method == "sendMessage"
        and "<b>Derp</b>" in str(call.fields.get("text", ""))
    )
    assert "<b>Derp</b>" in settings_call.fields["text"]

    privacy = await telegram_conversation.press_button(
        actor=actor,
        message=panel,
        text="Privacy & history",
    )
    assert str(privacy["text"]).startswith("Privacy and history\n")
    privacy_edit = next(
        call
        for call in reversed(telegram_conversation.server.calls)
        if call.method == "editMessageText"
    )
    assert "<b>Privacy and history</b>" in privacy_edit.fields["text"]
    assert any(
        call.method == "answerCallbackQuery"
        for call in telegram_conversation.server.calls
    )
    assert not model.calls
    model.assert_exhausted()


async def test_unpersisted_payment_is_redelivered_after_process_crash(
    telegram_conversation: TelegramConversation,
) -> None:
    actor = TelegramUser(id=7_704_001, first_name="Katherine", username="kat")
    chat = TelegramChat(id=actor.id, type="private", first_name=actor.first_name)
    await telegram_conversation.seed_paid_scope(actor=actor, chat=chat)
    blocked_inbox = BlockingPaymentInbox()
    await telegram_conversation.start(
        DeterministicChatModel.expecting_no_calls(),
        payment_update_inbox=blocked_inbox,
    )

    payment = await telegram_conversation.push_successful_payment(
        actor=actor,
        chat=chat,
    )
    await asyncio.wait_for(blocked_inbox.started.wait(), timeout=5)

    assert telegram_conversation.server.update_delivery_count(payment.update_id) == 1
    assert telegram_conversation.server.confirmed_offset < payment.update_id + 1

    await telegram_conversation.crash()
    assert telegram_conversation.server.confirmed_offset < payment.update_id + 1

    after_restart = DeterministicChatModel.expecting_no_calls()
    await telegram_conversation.start(after_restart)
    await telegram_conversation.server.wait_confirmed(payment.update_id)
    reply = await telegram_conversation.expect_reply(payment)

    assert "need review" in str(reply["text"]).lower()
    assert telegram_conversation.server.update_delivery_count(payment.update_id) == 2
    assert not after_restart.calls
    async with telegram_conversation.database.read_session() as session:
        inbox_rows = await session.scalar(
            select(func.count()).select_from(PaymentUpdateInbox)
        )
    assert inbox_rows == 1


async def test_paid_answer_has_reply_info_without_extra_inference(
    telegram_conversation: TelegramConversation,
) -> None:
    actor = TelegramUser(id=7_705_001, first_name="Margaret", username="margaret")
    chat = TelegramChat(id=actor.id, type="private", first_name=actor.first_name)
    await telegram_conversation.seed_paid_scope(actor=actor, chat=chat)
    model = DeterministicChatModel.from_responses("The release is ready for review.")
    await telegram_conversation.start(model)

    question = await telegram_conversation.send_text(
        actor=actor,
        chat=chat,
        text="Summarize the release state.",
    )
    answer = await telegram_conversation.expect_reply(
        question,
        text="The release is ready for review.",
        wait_persisted=True,
    )
    assert answer["text"] == "The release is ready for review."
    await telegram_conversation.wait_run_receipt(
        chat_id=chat.id,
        response_message_id=int(answer["message_id"]),
    )

    command = await telegram_conversation.send_text(
        actor=actor,
        chat=chat,
        text="/info",
        reply_to=answer,
        wait_persisted=False,
    )
    receipt = await telegram_conversation.expect_reply(command)

    assert str(receipt["text"]).startswith("About this answer\n")
    assert "Privacy: Private model" in str(receipt["text"])
    assert "Charged:" in str(receipt["text"])
    assert len(model.calls) == 1
    model.assert_exhausted()


async def test_support_reply_becomes_a_stable_case_not_chat_context(
    telegram_conversation: TelegramConversation,
) -> None:
    actor = TelegramUser(id=7_706_001, first_name="Dorothy", username="dorothy")
    chat = TelegramChat(id=actor.id, type="private", first_name=actor.first_name)
    user_model, _chat_model = await telegram_conversation.seed_paid_scope(
        actor=actor,
        chat=chat,
    )
    model = DeterministicChatModel.expecting_no_calls()
    await telegram_conversation.start(model)

    command = await telegram_conversation.send_text(
        actor=actor,
        chat=chat,
        text="/support",
        wait_persisted=False,
    )
    panel = await telegram_conversation.expect_message(
        command,
        text="Support\nChoose a topic, then send one short message.",
    )
    await telegram_conversation.press_button(
        actor=actor,
        message=panel,
        text="Privacy or data",
        expect_edit=False,
    )
    prompt = await telegram_conversation.server.wait_for_message(
        chat_id=chat.id,
        predicate=lambda message: (
            message.get("text") == "What happened? One message is enough."
        ),
    )
    note = await telegram_conversation.send_text(
        actor=actor,
        chat=chat,
        text="Please remove the stale profile detail from my account.",
        reply_to=prompt,
        wait_persisted=False,
    )
    case_message = await telegram_conversation.server.wait_for_message(
        chat_id=chat.id,
        predicate=lambda message: str(message.get("text", "")).startswith(
            "Support case open\n"
        ),
    )

    assert case_message["message_id"] == prompt["message_id"]
    async with telegram_conversation.database.read_session() as session:
        case = await session.scalar(
            select(SupportRequest).where(
                SupportRequest.requester_user_id == user_model.id
            )
        )
        remembered_note = await session.scalar(
            select(MessageModel.id).where(
                MessageModel.telegram_message_id == note.message_id
            )
        )
    assert case is not None
    assert case.description == "Please remove the stale profile detail from my account."
    assert remembered_note is None
    assert not model.calls
    model.assert_exhausted()


async def test_group_admin_enables_free_fallback_and_mentions_work_anywhere(
    telegram_conversation: TelegramConversation,
) -> None:
    actor = TelegramUser(id=7_707_001, first_name="Evelyn", username="evelyn")
    group = TelegramChat(
        id=-100_770_700,
        type="supergroup",
        title="Release room",
    )
    _user_model, chat_model = await telegram_conversation.seed_paid_scope(
        actor=actor,
        chat=group,
        member_status="administrator",
        credits=1,
    )
    model = DeterministicChatModel.from_responses(
        "Free fallback is enabled for this chat."
    )
    await telegram_conversation.start(model)

    command = await telegram_conversation.send_text(
        actor=actor,
        chat=group,
        text="/settings",
        wait_persisted=False,
    )
    panel = await telegram_conversation.expect_reply(command)
    review = await telegram_conversation.press_button(
        actor=actor,
        message=panel,
        text="Review free-model privacy",
    )
    await telegram_conversation.press_button(
        actor=actor,
        message=review,
        text="Allow free models in this chat",
    )

    mention = await telegram_conversation.send_text(
        actor=actor,
        chat=group,
        text="Could you confirm the fallback, derp, for everyone here?",
    )
    reply = await telegram_conversation.expect_reply(mention)

    assert reply["text"] == "Free fallback is enabled for this chat."
    assert len(model.calls) == 1
    assert model.calls[0].model_key == "free_text"
    async with telegram_conversation.database.read_session() as session:
        stored_chat = await session.get(ChatModel, chat_model.id)
    assert stored_chat is not None
    assert stored_chat.free_inference_enabled is True
    model.assert_exhausted()


async def test_unknown_command_recovers_without_history_or_inference(
    telegram_conversation: TelegramConversation,
) -> None:
    actor = TelegramUser(id=7_708_001, first_name="Joan", username="joan")
    chat = TelegramChat(id=actor.id, type="private", first_name=actor.first_name)
    await telegram_conversation.seed_paid_scope(actor=actor, chat=chat)
    model = DeterministicChatModel.expecting_no_calls()
    await telegram_conversation.start(model)

    command = await telegram_conversation.send_text(
        actor=actor,
        chat=chat,
        text="/settngs",
        wait_persisted=False,
    )
    reply = await telegram_conversation.expect_reply(
        command,
        text="I don't know that command. Try /help.",
    )

    assert reply["text"] == "I don't know that command. Try /help."
    async with telegram_conversation.database.read_session() as session:
        stored = await session.scalar(
            select(MessageModel.id)
            .join(ChatModel, ChatModel.id == MessageModel.chat_id)
            .where(
                ChatModel.telegram_id == chat.id,
                MessageModel.telegram_message_id == command.message_id,
            )
        )
    assert stored is None
    assert not model.calls
    model.assert_exhausted()
