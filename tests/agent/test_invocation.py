from typing import Any

import pytest
from aiogram.types import Message

from derp.agent.invocation import NAME, TriggerKind, detect_trigger

BOT = 4242
GROUP = {"id": -100, "type": "supergroup", "title": "g"}
HUMAN = {"id": 1, "is_bot": False, "first_name": "Ann"}


def msg(text: str = "", chat: dict[str, Any] | None = None, **extra: Any) -> Message:
    data: dict[str, Any] = {"message_id": 5, "date": 0, "chat": chat or GROUP, "from": HUMAN, **extra}
    if text:
        data["text"] = text
    return Message.model_validate(data)


def kind(message: Message) -> TriggerKind | None:
    trigger = detect_trigger(message, bot_id=BOT, bot_username="DerpRobot")
    return trigger.kind if trigger else None


@pytest.mark.parametrize(
    "text",
    [
        "derp, hi",
        "Derp's take?",
        "эй дерп",
        "Дерпу привет",
        "спроси у дерпа",
        "дерпик, ну",
        "Дерпуша!",
        "с дерпом",
    ],
)
def test_name_triggers(text: str) -> None:
    assert NAME.search(text)
    assert kind(msg(text)) is TriggerKind.NAME


@pytest.mark.parametrize("text", ["derpish vibes", "антидерп", "underp", "дерпнуть", "hello", "#derp-tag"])
def test_name_does_not_match_compounds(text: str) -> None:
    assert kind(msg(text)) is None


def test_mention_reply_private_and_commands() -> None:
    assert (
        kind(msg("@derprobot hi", entities=[{"type": "mention", "offset": 0, "length": 10}]))
        is TriggerKind.MENTION
    )
    reply = {
        "message_id": 3,
        "date": 0,
        "chat": GROUP,
        "from": {"id": BOT, "is_bot": True, "first_name": "Derp"},
    }
    assert kind(msg("lol", reply_to_message=reply)) is TriggerKind.REPLY
    assert (
        kind(msg("anything", chat={"id": 1, "type": "private", "first_name": "Ann"})) is TriggerKind.PRIVATE
    )
    command = [{"type": "bot_command", "offset": 0, "length": 5}]
    assert kind(msg("/help", entities=command)) is TriggerKind.COMMAND
    other = [{"type": "bot_command", "offset": 0, "length": 14}]
    assert kind(msg("/help@OtherBot", entities=other)) is None


def test_bots_never_trigger_but_anonymous_admins_do() -> None:
    bot = {"id": 9, "is_bot": True, "first_name": "Other"}
    assert kind(msg("derp hi", **{"from": bot})) is None
    anonymous = {"id": 1087968824, "is_bot": True, "first_name": "Group"}
    assert kind(msg("derp hi", sender_chat=GROUP, **{"from": anonymous})) is TriggerKind.NAME
    assert kind(msg("derp hi", is_automatic_forward=True)) is None
