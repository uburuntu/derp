"""Package imports remain independent from concrete runtime initialization."""

import subprocess
import sys


def test_image_adapter_can_load_before_concrete_tool_graph() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import derp.llm.image_executor; "
                "import derp.tools.policy; "
                "from derp.tools import create_chat_toolset; "
                "assert callable(create_chat_toolset)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
