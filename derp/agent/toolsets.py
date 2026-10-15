"""Which tools a run gets, by funding tier and chat.

Each tool family is one ``FunctionToolset[RunDeps]`` plus the tiers it is
allowed on (``derp.agent.tools.FAMILIES`` lists them all): paid runs get
everything, sponsored runs the cheap families, free-model runs only read-only
and Telegram-native ones.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic_ai.toolsets import AbstractToolset, FunctionToolset

from derp.agent.deps import Funding, RunDeps

ALL_TIERS = frozenset(Funding)


@dataclass(frozen=True, slots=True)
class Family:
    name: str
    toolset: FunctionToolset[RunDeps]
    tiers: frozenset[Funding] = ALL_TIERS
    available: Callable[[RunDeps], bool] = lambda _deps: True


def toolsets_for(deps: RunDeps, families: Sequence[Family]) -> list[AbstractToolset[RunDeps]]:
    return [f.toolset for f in families if deps.funding in f.tiers and f.available(deps)]
