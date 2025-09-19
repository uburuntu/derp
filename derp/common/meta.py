from __future__ import annotations

import re
from typing import Any

from aiogram.filters import BaseFilter
from aiogram.types import (
    Message,
)
from pydantic import BaseModel, Field


class MetaInfo(BaseModel):
    model_config = {
        "arbitrary_types_allowed": True,
        "str_strip_whitespace": True,
        "validate_assignment": True,
    }

    message: Message
    command: str | None = None
    hashtag: str | None = None
    arguments: list[str] = Field(default_factory=list)
    text: str

    @property
    def keyword(self) -> str:
        return self.command or self.hashtag or ""

    @property
    def target(self) -> Message:
        # Prefer the original message if it already contains text or
        # if it's not a pure text message.
        if self.text or (
            self.message.content_type and self.message.content_type != "text"
        ):
            return self.message

        if self.message.reply_to_message:
            return self.message.reply_to_message

        return self.message


class MetaCommand(BaseFilter):
    def __init__(self, *keywords: str, args: int | None = None):
        self.args = args
        self.prefixes = "/"
        self.ignore_case = True
        self.ignore_mention = False
        self.ignore_caption = False

        self.commands = (
            tuple(k.lower() for k in keywords) if self.ignore_case else keywords
        )

        # Prepare a robust hashtag pattern: non-word before #, then keyword, then _args
        p = "|".join(re.escape(k) for k in keywords)
        self.hashtags_pattern = re.compile(
            rf"(?<!\w)#({p})((?:_[\w\d]+)*)\b",
            re.IGNORECASE,
        )

    async def __call__(self, message: Message) -> bool | dict[str, Any]:
        text = message.text or (message.caption if not self.ignore_caption else None)
        if not text:
            return False

        me = await message.bot.me()

        def check_command(t: str) -> MetaInfo | None:
            split = t.split()
            full_command = split[0]
            prefix = full_command[0]
            command, _, mention = full_command[1:].partition("@")

            if (
                not self.ignore_mention
                and mention
                and me.username
                and me.username.lower() != mention.lower()
            ):
                # Mentioned bot is not the current bot
                return None

            if prefix not in self.prefixes:
                # Command prefix is not in the prefixes
                return None

            command_cmp = command.lower() if self.ignore_case else command
            if command_cmp not in self.commands:
                # Command is not in the commands
                return None

            command = command_cmp

            if self.args is None:
                arguments = split[1:]
                remainder = t[len(full_command) :]
                tail_text = remainder.lstrip()
            else:
                firsts = 1 + self.args
                arguments = split[1:firsts]
                texts = t.split(maxsplit=firsts)
                tail_text = texts[firsts] if firsts < len(texts) else ""

            return MetaInfo(
                message=message,
                command=command,
                arguments=arguments,
                text=tail_text,
            )

        def check_hashtag(t: str) -> MetaInfo | None:
            m = self.hashtags_pattern.search(t)
            if not m:
                return None
            h, args = m.group(1), m.group(2)
            arguments = [arg for arg in args.split("_") if arg]
            if self.args is not None:
                arguments = arguments[: self.args]
            # Remove only the first matched hashtag instance
            cleaned = self.hashtags_pattern.sub("", t, count=1).strip()
            return MetaInfo(
                message=message, hashtag=h, arguments=arguments, text=cleaned
            )

        meta = check_command(text) or check_hashtag(text)
        if meta is None:
            return False

        return {"meta": meta}
