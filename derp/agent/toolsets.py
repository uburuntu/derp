"""Which tools a run gets, by funding tier and chat.

Each tool family is one ``FunctionToolset[RunDeps]`` plus the tiers it is
allowed on (``derp.agent.tools.FAMILIES`` lists them all): paid runs get
everything, sponsored runs the cheap families, free-model runs only read-only
and Telegram-native ones. A handful of
core tools is always loaded; the long tail sits in the toolbox — deferred tools
the model finds through Pydantic AI's tool search (a ``search_tools`` call) so
their schemas stay out of small context windows.
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
    # Toolbox: True hides the whole family behind tool search, a tuple hides just those tools.
    deferred: bool | tuple[str, ...] = False

    def for_run(self) -> AbstractToolset[RunDeps]:
        if self.deferred is True:
            return self.toolset.defer_loading()
        if self.deferred:
            return self.toolset.defer_loading(list(self.deferred))
        return self.toolset


def toolsets_for(deps: RunDeps, families: Sequence[Family]) -> list[AbstractToolset[RunDeps]]:
    return [f.for_run() for f in families if deps.funding in f.tiers and f.available(deps)]
