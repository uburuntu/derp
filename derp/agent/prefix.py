"""Estimated size of what every request carries before the chat window: persona and tool schemas.

Turn prices (the least a payer must cover, and the fallback price of a cut-off
response) count it alongside the window; it is measured from the tool families
each tier gets, so it follows the toolset as tools come and go.
"""

import json
from collections.abc import Mapping, Sequence

from derp.agent.persona import PERSONA
from derp.agent.toolsets import Family
from derp.history.render import estimate_tokens
from derp.money.funding import Funding

# The situation block, Code Mode's run_code, the web server tools and the toolbox's search_tools.
FIXED_TOKENS = 800


def _hidden(family: Family, name: str, *, toolbox: bool) -> bool:
    if not toolbox:
        return False
    return name in family.deferred if isinstance(family.deferred, tuple) else family.deferred


def prefix_tokens(families: Sequence[Family], funding: Funding) -> int:
    """Persona + the schemas of every tool a run of this tier is offered up front + a fixed allowance."""
    toolbox = funding is Funding.FREE  # free runs keep deferred tools out of the prompt
    schemas = [
        json.dumps(
            {
                "name": tool.tool_def.name,
                "description": tool.tool_def.description,
                "parameters": tool.tool_def.parameters_json_schema,
            },
            ensure_ascii=False,
        )
        for family in families
        if funding in family.tiers
        for name, tool in family.toolset.tools.items()
        if not _hidden(family, name, toolbox=toolbox)
    ]
    return estimate_tokens(PERSONA) + sum(estimate_tokens(s) for s in schemas) + FIXED_TOKENS


def prefixes(families: Sequence[Family]) -> Mapping[Funding, int]:
    return {funding: prefix_tokens(families, funding) for funding in Funding}
