"""Contract tests for Pydantic AI composition."""

from pydantic_ai import models

from derp.llm.agents import (
    create_chat_agent,
    create_image_agent,
    create_inline_agent,
)
from derp.llm.providers import ModelTier
from derp.tools.toolsets import create_chat_toolset


def test_agent_factories_support_pydantic_ai_v2() -> None:
    assert create_chat_agent(ModelTier.CHEAP).name == "chat"
    assert create_image_agent().name == "image"
    assert create_inline_agent().name == "inline"


def test_chat_toolset_has_one_policy_aware_tool_per_capability() -> None:
    toolset = create_chat_toolset()

    assert set(toolset.tools) == {
        "edit_image",
        "generate_image",
        "think_deep",
        "update_chat_memory",
        "video_generate",
        "voice_tts",
        "web_search",
    }


def test_real_model_requests_are_disabled_in_tests() -> None:
    assert models.ALLOW_MODEL_REQUESTS is False
