"""Run-scoped dependencies handed to every tool.

Tools read the chat, the asker and the reply target from here — never from
their own arguments — so a prompt can't point a tool at another chat.
"""

from dataclasses import dataclass, field
from datetime import tzinfo

from aiogram import Bot
from pydantic_ai.models import Model

from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat
from derp.inference.api import OpenRouterAPI
from derp.inference.pins import PinStore
from derp.inference.roles import RolePin
from derp.money.funding import Funding, Funds
from derp.telegram.media import MediaGateway
from derp.telegram.sender import Target
from derp.tools.lookups import Lookups


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
    funds: Funds  # who pays and how much is held: the run's money cap
    target: Target
    tz: tzinfo
    gateway: MediaGateway
    pin: RolePin
    model: Model
    digest_pin: RolePin  # a cheap pin for day digests
    digest_model: Model
    digest_role: str
    api: OpenRouterAPI
    pins: PinStore
    lookups: Lookups
    history_start: int = 0  # index of the run's first message in the agent's history
    delivered: list[tuple[int, str]] = field(
        default_factory=list[tuple[int, str]]
    )  # (message id, kind) so far
    reacted: set[int] = field(default_factory=set[int])  # messages Derp reacted to with the react tool
    search_withheld: bool = False  # the hold can't cover a web search in the coming request

    @property
    def funding(self) -> Funding:
        return self.funds.funding

    @property
    def tool_outputs_left(self) -> int:
        """Visible outputs tools may still send; one slot stays free for the final text answer."""
        return self.settings.run_caps.visible_outputs - 1 - len(self.delivered)
