"""Tool families available to the agent; ``toolsets_for`` filters them per run."""

from derp.agent.tools import history, images, memory, telegram
from derp.agent.toolsets import Family

FAMILIES: tuple[Family, ...] = (
    history.family,
    memory.family,
    telegram.family,
    telegram.ops_family,
    images.family,
)
