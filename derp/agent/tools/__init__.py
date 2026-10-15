"""Tool families available to the agent; ``toolsets_for`` filters them per run."""

from derp.agent.tools import history
from derp.agent.toolsets import Family

FAMILIES: tuple[Family, ...] = (history.family,)
