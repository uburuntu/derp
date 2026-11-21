"""When does a message invoke Derp?

Private chats: every message. Groups: an @mention (or text mention) of Derp,
a reply to one of Derp's messages, or Derp's name in English or Russian
(case endings and diminutives, but not compounds like «антидерп» or
"derpish"). Messages from bots never trigger (bot-to-bot loop guard);
anonymous admins posting as the group itself do. A standalone name deterministically invokes Derp.
"""

import re
from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from aiogram.types import Message, MessageEntity
from pydantic import JsonValue

from derp.history.capture import rich_text

NAME = re.compile(
    r"(?<![\w@-])(?:"
    r"derp(?:'s)?"
    r"|дерп(?:ик(?:а|у|ом|е|и|ов)?|уш(?:а|и|е|у|ей|ка|ки|ке|ку|кой)?|а|у|ом|е|ы|ов|ам|ами|ах)?"
    r")(?![\w-])",
    re.IGNORECASE,
)


class TriggerKind(StrEnum):
    PRIVATE = "private"
    MENTION = "mention"
    REPLY = "reply"
    NAME = "name"
    COMMAND = "command"


@dataclass(frozen=True, slots=True)
class Trigger:
    kind: TriggerKind


def message_text(message: Message) -> str:
    return message.text or message.caption or rich_text(message) or ""


def _entities(message: Message) -> list[MessageEntity]:
    return list(message.entities or message.caption_entities or [])


def _rich_mention(message: Message, bot_id: int, usernames: Collection[str]) -> bool:
    """Rich mentions have their own target, independent of the label shown to readers."""
    if message.rich_message is None:
        return False
    nodes = [cast(JsonValue, message.rich_message.model_dump(mode="json", exclude_none=True))]
    while nodes:
        node = nodes.pop()
        if isinstance(node, list):
            nodes.extend(node)
        elif isinstance(node, dict):
            if node.get("type") == "mention" and str(node.get("username", "")).casefold() in usernames:
                return True
            user = node.get("user")
            if node.get("type") == "text_mention" and isinstance(user, dict) and user.get("id") == bot_id:
                return True
            nodes.extend(node.values())
    return False


def _command_target(message: Message, entity: MessageEntity) -> str | None:
    command = entity.extract_from(message_text(message))
    return command.partition("@")[2] or None


def starts_with_command(message: Message) -> bool:
    """A bot command (for Derp or any other bot): a control, never conversation."""
    entities = _entities(message)
    return bool(entities) and entities[0].type == "bot_command" and entities[0].offset == 0


def bot_authored(message: Message) -> bool:
    """Telegram uses a fake sender for posts on behalf of a chat. A human can also send via an inline bot."""
    return (
        message.sender_business_bot is not None
        or message.guest_bot_caller_user is not None
        or message.guest_bot_caller_chat is not None
        or (
            message.from_user is not None
            and message.from_user.is_bot
            and (message.sender_chat is None or message.sender_chat.id != message.chat.id)
        )
    )


def from_human(message: Message) -> bool:
    if message.is_automatic_forward or bot_authored(message):
        return False
    sender = message.from_user
    if sender is None:
        return False
    if sender.is_bot:
        # Anonymous group admins post as the group via a service bot account.
        return message.sender_chat is not None and message.sender_chat.id == message.chat.id
    return True


def detect_trigger(message: Message, *, bot_id: int, usernames: Collection[str]) -> Trigger | None:
    """``usernames``: every @username of the bot (``telegram.bot.bot_usernames``)."""
    usernames = {name.casefold() for name in usernames}
    if not from_human(message) or message.guest_query_id:
        return None
    text = message_text(message)
    entities = _entities(message)
    if entities and entities[0].type == "bot_command" and entities[0].offset == 0:
        target = _command_target(message, entities[0])
        if target is None or target.casefold() in usernames:
            return Trigger(TriggerKind.COMMAND)
        return None
    if message.chat.type == "private":
        return Trigger(TriggerKind.PRIVATE)
    for entity in entities:
        if entity.type == "mention" and entity.extract_from(text).removeprefix("@").casefold() in usernames:
            return Trigger(TriggerKind.MENTION)
        if entity.type == "text_mention" and entity.user is not None and entity.user.id == bot_id:
            return Trigger(TriggerKind.MENTION)
    if _rich_mention(message, bot_id, usernames):
        return Trigger(TriggerKind.MENTION)
    replied = message.reply_to_message
    if replied is not None and replied.from_user is not None and replied.from_user.id == bot_id:
        return Trigger(TriggerKind.REPLY)
    if NAME.search(text):
        return Trigger(TriggerKind.NAME)
    return None
