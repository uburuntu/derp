from typing import Any, cast

from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openai import OpenAIChatModelSettings
from pydantic_ai.native_tools import WebFetchTool, WebSearchTool
from pydantic_ai.providers.openrouter import OpenRouterProvider

from derp.inference.openrouter import DerpOpenRouterModel


def test_web_fetch_and_search_become_openrouter_server_tools() -> None:
    model = DerpOpenRouterModel("test/model", provider=OpenRouterProvider(api_key="test-key"))
    parameters = ModelRequestParameters(
        native_tools=[
            WebSearchTool(search_context_size="low", max_uses=2),
            WebFetchTool(max_uses=3, max_content_tokens=6000),
        ]
    )
    tools, _choice = model._get_tool_choice(OpenAIChatModelSettings(), parameters)
    by_type = {cast(dict[str, Any], tool)["type"]: cast(dict[str, Any], tool) for tool in tools}
    assert by_type["openrouter:web_search"]["parameters"] == {"search_context_size": "low", "max_uses": 2}
    assert by_type["openrouter:web_fetch"]["parameters"] == {
        "engine": "openrouter",
        "max_uses": 3,
        "max_content_tokens": 6000,
    }
