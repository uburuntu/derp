"""Which tools a run gets, by funding tier and chat.

Each tool family registers one ``FunctionToolset[RunDeps]`` with the tiers it
is allowed on: paid runs get everything, sponsored runs the cheap families,
free-model runs only read-only and Telegram-native ones.
"""

from collections.abc import Callable
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


FAMILIES: list[Family] = []


def register(family: Family) -> Family:
    FAMILIES.append(family)
    return family


def toolsets_for(deps: RunDeps) -> list[AbstractToolset[RunDeps]]:
    return [f.toolset for f in FAMILIES if deps.funding in f.tiers and f.available(deps)]
