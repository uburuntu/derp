"""Long-form Telegram conversations through polling, HTTP, and PostgreSQL."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from derp.models import Chat as ChatModel
from derp.models import InferenceUsage
from derp.models import Message as MessageModel
from tests.e2e.harness import (
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
        reply = await telegram_conversation.expect_reply(incoming)
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
    resumed_reply = await telegram_conversation.expect_reply(resumed)

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

    await telegram_conversation.send_text(
        actor=actor,
        chat=forum,
        thread_id=11,
        text="The dashboard deploy is Tuesday.",
    )
    await telegram_conversation.send_text(
        actor=actor,
        chat=forum,
        thread_id=22,
        text="The signing key rotation is Thursday.",
    )
    topic_11_question = await telegram_conversation.send_text(
        actor=actor,
        chat=forum,
        thread_id=11,
        text="Derp, what is this topic's schedule?",
    )
    topic_11_reply = await telegram_conversation.expect_reply(topic_11_question)
    topic_22_question = await telegram_conversation.send_text(
        actor=actor,
        chat=forum,
        thread_id=22,
        text="Derp, what is this topic's schedule?",
    )
    topic_22_reply = await telegram_conversation.expect_reply(topic_22_question)

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
    model = DeterministicChatModel.from_responses("unused")
    await telegram_conversation.start(model)

    command = await telegram_conversation.send_text(
        actor=actor,
        chat=chat,
        text="/settings",
    )
    panel = await telegram_conversation.expect_reply(command)
    assert "<b>Derp</b>" in panel["text"]

    privacy = await telegram_conversation.press_button(
        actor=actor,
        message=panel,
        text="Privacy & history",
    )
    assert "<b>Privacy and history</b>" in privacy["text"]
    assert any(
        call.method == "answerCallbackQuery"
        for call in telegram_conversation.server.calls
    )
    assert not model.calls
