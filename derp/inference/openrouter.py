"""The provider seam: role pins → Pydantic AI OpenRouter models, and cost extraction.

Cost is OpenRouter's reported ``usage.cost`` (``provider_details['cost']``),
which already includes server-tool fees such as web search. Never use
``ModelResponse.cost()``: it estimates from a static price table.
"""

import hashlib
import hmac
import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast, override

from openai.types import chat
from openai.types.chat import ChatCompletionToolChoiceOptionParam
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.openai import OpenAIChatModelSettings
from pydantic_ai.models.openrouter import OpenRouterModel, OpenRouterModelSettings, OpenRouterProviderConfig
from pydantic_ai.native_tools import AbstractNativeTool, WebFetchTool
from pydantic_ai.providers.openrouter import OpenRouterProvider

from derp.config import Settings
from derp.inference.roles import RolePin


def build_provider(settings: Settings) -> OpenRouterProvider:
    return OpenRouterProvider(
        api_key=settings.openrouter_api_key,
        app_url=settings.openrouter_app_url,
        app_title=settings.openrouter_app_title,
    )


# Fast routing preferences (p50 over OpenRouter's rolling 5 minutes); soft, unlike max_price.
MIN_THROUGHPUT = {"p50": 80}  # tokens per second
MAX_LATENCY = {"p50": 1.5}  # seconds to the first token


def provider_config(pin: RolePin) -> OpenRouterProviderConfig:
    config: OpenRouterProviderConfig = {}
    if pin.zdr:
        config["zdr"] = True
        config["data_collection"] = "deny"
    if pin.fast:  # documented provider fields Pydantic AI's TypedDict doesn't list yet; passed through as is
        extra = cast(dict[str, Any], config)
        extra["preferred_min_throughput"] = MIN_THROUGHPUT
        extra["preferred_max_latency"] = MAX_LATENCY
    max_price: dict[str, float] = {}
    if pin.max_price_prompt is not None:
        max_price["prompt"] = pin.max_price_prompt
    if pin.max_price_completion is not None:
        max_price["completion"] = pin.max_price_completion
    if max_price:
        config["max_price"] = cast(Any, max_price)
    return config


# Pinned per request: "auto" falls back to Exa ($0.007) for models without native search, which most
# chat pins are; Parallel "fast" is $0.001 and handles Russian as well as English.
SEARCH_ENGINE = {"engine": "parallel", "mode": "fast"}


class DerpOpenRouterModel(OpenRouterModel):
    """OpenRouter with a pinned web-search engine, plus the ``openrouter:web_fetch`` server tool.

    Pydantic AI maps ``WebSearchTool`` but can't choose its engine, and doesn't map web fetch yet.
    Fetches use the plain ``openrouter`` engine (direct HTTP fetch + extraction, no third-party
    backend); nothing is fetched from Derp's own host.
    """

    @classmethod
    @override
    def supported_native_tools(cls) -> frozenset[type[AbstractNativeTool]]:
        return super().supported_native_tools() | {WebFetchTool}

    @override
    def _get_tool_choice(
        self, model_settings: OpenAIChatModelSettings, model_request_parameters: ModelRequestParameters
    ) -> tuple[list[chat.ChatCompletionToolParam], ChatCompletionToolChoiceOptionParam | None]:
        tools, choice = super()._get_tool_choice(model_settings, model_request_parameters)
        for tool in tools:
            if tool.get("type") == "openrouter:web_search":
                cast(dict[str, Any], tool)["parameters"] |= SEARCH_ENGINE
        fetch = next((t for t in model_request_parameters.native_tools if isinstance(t, WebFetchTool)), None)
        if fetch is not None:
            parameters: dict[str, Any] = {"engine": "openrouter"}
            if fetch.max_uses is not None:
                parameters["max_uses"] = fetch.max_uses
            if fetch.max_content_tokens is not None:
                parameters["max_content_tokens"] = fetch.max_content_tokens
            if fetch.blocked_domains:
                parameters["blocked_domains"] = fetch.blocked_domains
            tools.append(
                cast(chat.ChatCompletionToolParam, {"type": "openrouter:web_fetch", "parameters": parameters})
            )
        return tools, choice


# Keys conversation ids so OpenRouter never sees Telegram ids; per process, so a restart only resets stickiness.
_SESSION_KEY = secrets.token_bytes(16)


def conversation_id(scope: str) -> str:
    """OpenRouter ``session_id`` for a chat topic or a person: keeps them on one provider, whose cache is warm."""
    return hmac.new(_SESSION_KEY, scope.encode(), hashlib.sha256).hexdigest()[:32]


type ModelFactory = Callable[[RolePin], Model]  # ``chat_model`` bound to the provider (tests script it)


def chat_model(pin: RolePin, provider: OpenRouterProvider) -> OpenRouterModel:
    return DerpOpenRouterModel(pin.model, provider=provider)


def chat_settings(pin: RolePin, *, max_tokens: int, session: str | None = None) -> OpenRouterModelSettings:
    """Per-request settings: fallbacks, per-request ZDR, usage accounting, prompt caching.

    ``session`` (a ``conversation_id``) makes OpenRouter's provider routing sticky for that conversation;
    without it the conversation is keyed by the first system and user messages.

    No ``sort: price`` or ``:floor`` on chat roles: OpenRouter's default routing
    keeps tool-calling quality (Exacto) which matters more than the last cent.
    """
    settings = OpenRouterModelSettings(
        max_tokens=max_tokens,
        openrouter_usage={"include": True},
        openrouter_provider=provider_config(pin),
        openrouter_cache_instructions=True,
        openrouter_cache_tool_definitions=True,
    )
    if pin.fallbacks:
        settings["openrouter_models"] = list(pin.fallbacks)
    if session is not None:
        settings["extra_body"] = {"session_id": session}
    if pin.reasoning == "none":
        # Some providers ignore effort "none"; only an explicit disable stops hidden reasoning.
        settings["openrouter_reasoning"] = {"enabled": False}
    elif pin.reasoning is not None:
        settings["openrouter_reasoning"] = {"effort": pin.reasoning, "exclude": True}
    return settings


@dataclass(frozen=True, slots=True)
class CallUsage:
    """What one provider response cost and who served it."""

    cost_usd: Decimal
    model: str | None
    downstream_provider: str | None
    generation_id: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    reasoning_tokens: int
    web_search_requests: int


def response_usage(response: ModelResponse) -> CallUsage:
    details: Mapping[str, Any] = response.provider_details or {}
    usage = response.usage
    server_tools: Mapping[str, Any] = details.get("server_tool_use") or {}
    return CallUsage(
        # 0 only for free models: paid responses without a cost are priced first (agent.runs.price_unreported)
        cost_usd=Decimal(str(details.get("cost") or 0)),
        model=response.model_name,
        downstream_provider=details.get("downstream_provider"),
        generation_id=response.provider_response_id,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        reasoning_tokens=int(usage.details.get("reasoning_tokens", 0)),
        web_search_requests=int(server_tools.get("web_search_requests") or 0),
    )


def responses(messages: Iterable[ModelMessage]) -> list[ModelResponse]:
    return [message for message in messages if isinstance(message, ModelResponse)]


def total_cost(messages: Iterable[ModelMessage]) -> Decimal:
    return sum((response_usage(r).cost_usd for r in responses(messages)), Decimal(0))
