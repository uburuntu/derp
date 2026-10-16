"""Which tools a run gets, by funding tier and chat.

Each tool family is one ``FunctionToolset[RunDeps]`` plus the tiers it is
allowed on (``derp.agent.tools.FAMILIES`` lists them all): paid runs get
everything, sponsored runs the cheap families, free-model runs only read-only
and Telegram-native ones.

Paid and sponsored runs load every tool they may use: on OpenRouter, tool search
reveals tools by changing the tool list, which sits at the start of the prompt,
so each discovery would cost an extra model turn and void the cached prefix
(more schemas in a cached prefix cost far less). Free-model runs keep the long
tail in the toolbox — deferred tools found through Pydantic AI's local
``search_tools`` — so their schemas stay out of small context windows.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic_ai.toolsets import AbstractToolset, FunctionToolset

from derp.agent.deps import RunDeps
from derp.money.funding import Funding

ALL_TIERS = frozenset(Funding)


@dataclass(frozen=True, slots=True)
class Family:
    name: str
    toolset: FunctionToolset[RunDeps]
    tiers: frozenset[Funding] = ALL_TIERS
    available: Callable[[RunDeps], bool] = lambda _deps: True
    # Toolbox: True hides the whole family behind tool search, a tuple hides just those tools.
    deferred: bool | tuple[str, ...] = False

    def for_run(self, *, toolbox: bool = True) -> AbstractToolset[RunDeps]:
        if not toolbox or not self.deferred:
            return self.toolset
        if self.deferred is True:
            return self.toolset.defer_loading()
        return self.toolset.defer_loading(list(self.deferred))


def toolsets_for(deps: RunDeps, families: Sequence[Family]) -> list[AbstractToolset[RunDeps]]:
    toolbox = deps.funding is Funding.FREE
    return [f.for_run(toolbox=toolbox) for f in families if deps.funding in f.tiers and f.available(deps)]
