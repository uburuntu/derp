from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Message


class SkipAnonymousMessagesMiddleware(BaseMiddleware):
    """
    Allows to have messages with correct `from_user` field

    Docs: https://core.telegram.org/bots/api-changelog#november-4-2020
    """

    user_id_auto_forwards = 777000
    user_id_anonymous_sender = 1087968824

    reply_interval = timedelta(hours=1)

    def __init__(self):
        self.last_reply_dt = defaultdict(lambda: datetime.utcfromtimestamp(0))

    async def reply_once_in_interval(self, message: Message):
        now_dt = datetime.utcnow()

        if now_dt - self.last_reply_dt[message.chat.id] < self.reply_interval:
            return

        self.last_reply_dt[message.chat.id] = now_dt

        return await message.reply("🥷 He кекаю c анонимами")

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: dict[str, Any],
    ) -> Any:
        # Just skip auto-forwards from channels to linked chat
        if event.from_user.id == self.user_id_auto_forwards:
            return UNHANDLED

        # But reply to manually created messages before skip
        if event.sender_chat:
            return await self.reply_once_in_interval(event)

        return await handler(event, data)
