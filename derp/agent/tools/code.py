"""Python in a sandbox: the model's calculator and scratchpad.

``run_code`` runs a Python subset in Monty (via Pydantic AI Harness Code Mode)
in a local worker process: no third-party imports, files, environment, clock
or network, with time and memory caps. No Derp tools are callable from inside
it, so it can only compute. Its output is clamped with the other tool outputs
(``derp.agent.tools.OUTPUT_LIMITS``).
"""

from pydantic_ai_harness import CodeMode

SECONDS = 5  # sandbox execution time per snippet
MEMORY = 64 * 1024 * 1024

CODE_MODE = CodeMode[object](
    tools=[], max_retries=2, resource_limits={"max_duration_secs": SECONDS, "max_memory": MEMORY}
)
