"""Python in a sandbox: the model's calculator and scratchpad.

``run_code`` runs a Python subset in Monty (via Pydantic AI Harness Code Mode)
in a local worker process: no third-party imports, files, environment, clock
or network, with time and memory caps. No Derp tools are callable from inside
it, so it can only compute. Its output is clamped before it reaches the model.
"""

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai_harness import CodeMode, ToolOutputLimits
from pydantic_ai_harness.tool_output_limits import Band, Truncate

SECONDS = 5  # sandbox execution time per snippet
MEMORY = 64 * 1024 * 1024
OUTPUT_CHARS = 4000

CODE_CAPABILITIES: list[AbstractCapability[object]] = [
    CodeMode(tools=[], max_retries=2, resource_limits={"max_duration_secs": SECONDS, "max_memory": MEMORY}),
    ToolOutputLimits(
        bands=[], per_tool={"run_code": [Band(over=OUTPUT_CHARS, action=Truncate(max_chars=OUTPUT_CHARS))]}
    ),
]
