"""Python in a sandbox: the model's calculator and scratchpad.

``run_code`` runs a Python subset in Monty (via Pydantic AI Harness Code Mode)
in a local worker process: no third-party imports, files, environment, clock
or network, with time and memory caps. No Derp tools are callable from inside
it, so it can only compute. Its output is clamped with the other tool outputs
(``derp.agent.tools.OUTPUT_LIMITS``). Code Mode's stock description is written
for coding agents (REPL semantics, return envelopes: about 600 tokens in every
request); ``Sandbox`` sends a brief one instead.
"""

from dataclasses import replace

from pydantic_ai import RunContext
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, PreparedToolset
from pydantic_ai_harness import CodeMode

SECONDS = 5  # sandbox execution time per snippet
MEMORY = 64 * 1024 * 1024
BRIEF = (
    "Run Python for exact arithmetic, dates and data crunching. A sandboxed subset: standard library only "
    "(math, random, re, json, datetime, collections, itertools, functools, unicodedata; no decimal, "
    "statistics or numpy), no files, network or clock. The last expression is the result; variables persist "
    "between calls."
)


def _brief(_ctx: RunContext[object], tool_defs: list[ToolDefinition]) -> list[ToolDefinition]:
    return [replace(d, description=BRIEF) if d.name == "run_code" else d for d in tool_defs]


class Sandbox(CodeMode[object]):
    # ``run_code`` is made by Code Mode's wrapper toolset, outside the reach of ``prepare_tools`` hooks.
    def get_wrapper_toolset(self, toolset: AbstractToolset[object]) -> AbstractToolset[object] | None:
        return PreparedToolset(super().get_wrapper_toolset(toolset) or toolset, _brief)


CODE_MODE = Sandbox(
    tools=[], max_retries=2, resource_limits={"max_duration_secs": SECONDS, "max_memory": MEMORY}
)
