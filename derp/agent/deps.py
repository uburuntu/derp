"""Run-scoped dependencies handed to every tool.

Tools read the chat, the asker and the reply target from here — never from
their own arguments — so a prompt can't point a tool at another chat.
"""

from dataclasses import dataclass, field
from datetime import tzinfo
from typing import TYPE_CHECKING

from aiogram import Bot
from pydantic_ai.models import Model

from derp.config import Settings
from derp.db.engine import Database
from derp.history.models import Chat
from derp.inference.api import OpenRouterAPI
from derp.inference.openrouter import ModelFactory
from derp.inference.pins import PinStore
from derp.inference.roles import RolePin
from derp.money.funding import Funding, Funds
from derp.telegram.media import MediaGateway
from derp.telegram.sender import Target
from derp.tools.lookups import Lookups
from derp.tools.workers import WorkerPool

if TYPE_CHECKING:
    from derp.agent.outputs import Kept


@dataclass(slots=True)
class RunDeps:
    db: Database
    bot: Bot
    settings: Settings
    chat: Chat
    user_id: int
    locale: str
    trigger_message_id: int
    run_id: int
    funds: Funds  # who pays and how much is held: the run's money cap
    target: Target
    tz: tzinfo
    gateway: MediaGateway
    pin: RolePin
    model: Model
    model_factory: ModelFactory  # models for other pins (day digests)
    api: OpenRouterAPI
    pins: PinStore
    lookups: Lookups
    workers: WorkerPool  # local media effects
    history_start: int = 0  # index of the run's first message in the agent's history
    # Messages whose images the run looks at (``media.run_media``): what edits and effects start from.
    seen: tuple[int, ...] = ()
    delivered: list[tuple[int, str]] = field(
        default_factory=list[tuple[int, str]]
    )  # (message id, kind) so far
    riders: int = 0  # delivered messages that went out in another one's slot (a poll series' later polls)
    kept: dict[str, Kept] = field(default_factory=dict[str, "Kept"])  # results kept for the next tool, by ref
    reacted: set[int] = field(default_factory=set[int])  # messages Derp reacted to with the react tool
    detached: bool = False  # an inline or guest answer: text only, nothing is posted

    @property
    def funding(self) -> Funding:
        return self.funds.funding

    @property
    def side_funding(self) -> str:
        """Who a side call on Derp's own pins (a digest) is ledgered to: the run's payer, or ``creator`` on a
        key run with no hold behind it (a cheap call, absorbed rather than charged to nobody)."""
        if self.funds.key is not None and self.funds.reservation_id is None:
            return "creator"
        return self.funding.value

    @property
    def tool_outputs_left(self) -> int:
        """Visible outputs tools may still send; one slot stays free for the final text answer.

        A kept result holds a slot: it is posted at the end unless a tool uses it.
        """
        waiting = sum(not kept.used for kept in self.kept.values())
        taken = len(self.delivered) - self.riders
        return self.settings.run_caps.visible_outputs - 1 - taken - waiting
