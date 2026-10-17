from derp.agent.persona import PERSONA
from derp.agent.prefix import FIXED_TOKENS, prefix_tokens, prefixes
from derp.agent.tools import FAMILIES
from derp.history.render import estimate_tokens
from derp.money.funding import Funding


def test_the_prompt_prefix_counts_persona_and_the_tools_each_tier_is_offered() -> None:
    sizes = prefixes(FAMILIES)
    floor = estimate_tokens(PERSONA) + FIXED_TOKENS
    assert all(size > floor for size in sizes.values())
    # Free runs keep the long tail behind tool search, so their prompt is the smallest.
    assert sizes[Funding.FREE] < sizes[Funding.SPONSORED] <= sizes[Funding.PAID]
    assert prefix_tokens((), Funding.PAID) == floor
