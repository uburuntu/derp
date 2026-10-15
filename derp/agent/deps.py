"""Run-scoped dependencies handed to every tool.

Tools read the chat, the asker and the reply target from here — never from
their own arguments — so a prompt can't point a tool at another chat.
"""

from dataclasses import dataclass, field
from datetime import tzinfo
from enum import StrEnum

from aiogram import Bot
from pydantic_ai.models import Model

from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat
from derp.inference.roles import ModelRole, RolePin
from derp.telegram.media import MediaGateway
from derp.telegram.sender import Target


class Funding(StrEnum):
    PAID = "paid"
    SPONSORED = "sponsored"
    FREE = "free"

    @property
    def role(self) -> ModelRole:
        return {
            Funding.PAID: ModelRole.CHAT,
            Funding.SPONSORED: ModelRole.CHAT_SPONSORED,
            Funding.FREE: ModelRole.CHAT_FREE,
        }[self]


@dataclass(slots=True)
class RunDeps:
    db: Database
    bot: Bot
    settings: Settings
    chat: Chat
    user_id: int
    asker: str
    locale: str
    thread_id: int | None
    trigger_message_id: int
    run_id: int
    funding: Funding
    target: Target
    tz: tzinfo
    gateway: MediaGateway
    pin: RolePin
    model: Model
    delivered: list[int] = field(default_factory=list[int])  # message ids of visible outputs so far

    @property
    def outputs_left(self) -> int:
        return self.settings.run_caps.visible_outputs - len(self.delivered)
