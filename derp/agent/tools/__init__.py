"""Tool families available to the agent (``toolsets.offered`` picks them per run) and run-wide capabilities."""

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai_harness import ToolOutputLimits
from pydantic_ai_harness.repair_tool_arguments import RepairToolArguments
from pydantic_ai_harness.tool_output_limits import Band, Truncate, TruncationStrategy

from derp.agent.deps import RunDeps
from derp.agent.tools import (
    about,
    effects,
    games,
    history,
    images,
    memory,
    quotes,
    scheduler,
    self_service,
    telegram,
    utilities,
)
from derp.agent.tools.code import CODE_MODE
from derp.agent.tools.web import WEB_CAPABILITIES
from derp.agent.toolsets import Family

FAMILIES: tuple[Family, ...] = (
    about.family,
    about.source_family,
    history.family,
    memory.family,
    telegram.family,
    telegram.ops_family,
    images.family,
    scheduler.family,
    games.family,
    self_service.family,
    utilities.family,
    utilities.wolfram_family,
    utilities.code_family,
    effects.family,
    effects.background_family,
    effects.song_family,
    effects.profile_family,
    quotes.family,
)


def _clamp(chars: int) -> list[Band]:
    return [Band(over=chars, action=Truncate(max_chars=chars, strategy=TruncationStrategy.head))]


# Tool results that can grow with the chat or the code are clamped once, when produced.
OUTPUT_LIMITS = ToolOutputLimits[RunDeps](
    bands=[],
    per_tool={
        "run_code": _clamp(4000),
        "read_source": _clamp(12000),
        **{name: _clamp(8000) for name in ("read_messages", "search_history", "chat_query", "digest")},
    },
)
# Cheap models' malformed tool-call JSON is repaired before validation.
CAPABILITIES: list[AbstractCapability[RunDeps]] = [
    *WEB_CAPABILITIES,
    CODE_MODE,
    OUTPUT_LIMITS,
    RepairToolArguments[RunDeps](),
]
