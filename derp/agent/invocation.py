"""When does a message invoke Derp?

Private chats: every message. Groups: an @mention (or text mention) of Derp,
a reply to one of Derp's messages, or Derp's name in English or Russian
(case endings and diminutives, but not compounds like «антидерп» or
"derpish"). Messages from bots never trigger (bot-to-bot loop guard);
anonymous admins posting as the group itself do.
"""

import re
from dataclasses import dataclass
from enum import StrEnum

from aiogram.types import Message, MessageEntity

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
    return message.text or message.caption or ""


def _entities(message: Message) -> list[MessageEntity]:
    return list(message.entities or message.caption_entities or [])


def _command_target(message: Message, entity: MessageEntity) -> str | None:
    command = message_text(message)[entity.offset : entity.offset + entity.length]
    return command.partition("@")[2] or None


def from_human(message: Message) -> bool:
    if message.is_automatic_forward:
        return False
    sender = message.from_user
    if sender is None:
        return False
    if sender.is_bot:
        # Anonymous group admins post as the group via a service bot account.
        return message.sender_chat is not None and message.sender_chat.id == message.chat.id
    return True


def detect_trigger(message: Message, *, bot_id: int, bot_username: str) -> Trigger | None:
    if not from_human(message) or message.guest_query_id:
        return None
    text = message_text(message)
    entities = _entities(message)
    username = bot_username.casefold()
    if entities and entities[0].type == "bot_command" and entities[0].offset == 0:
        target = _command_target(message, entities[0])
        if target is None or target.casefold() == username:
            return Trigger(TriggerKind.COMMAND)
        return None
    if message.chat.type == "private":
        return Trigger(TriggerKind.PRIVATE)
    for entity in entities:
        if (
            entity.type == "mention"
            and text[entity.offset + 1 : entity.offset + entity.length].casefold() == username
        ):
            return Trigger(TriggerKind.MENTION)
        if entity.type == "text_mention" and entity.user is not None and entity.user.id == bot_id:
            return Trigger(TriggerKind.MENTION)
    replied = message.reply_to_message
    if replied is not None and replied.from_user is not None and replied.from_user.id == bot_id:
        return Trigger(TriggerKind.REPLY)
    if NAME.search(text):
        return Trigger(TriggerKind.NAME)
    return None
