"""Tool families available to the agent; ``toolsets_for`` filters them per run."""

from derp.agent.tools import games, history, images, memory, scheduler, telegram, utilities
from derp.agent.toolsets import Family

FAMILIES: tuple[Family, ...] = (
    history.family,
    memory.family,
    telegram.family,
    telegram.ops_family,
    images.family,
    scheduler.family,
    games.family,
    utilities.family,
    utilities.wolfram_family,
    utilities.code_family,
)
