from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, get_type_hints

import logfire
from google import genai
from google.genai import types
from google.genai.types import GenerateContentResponse

from ..config import settings


@dataclass(frozen=True, slots=True)
class GeminiResult:
    """Represents a structured result from a Gemini API call."""

    text_parts: list[str] = field(default_factory=list)
    code_blocks: list[str] = field(default_factory=list)
    execution_results: list[str] = field(default_factory=list)
    images: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_content(self) -> bool:
        """Check if the result has any content."""
        return any(
            [self.text_parts, self.code_blocks, self.execution_results, self.images]
        )

    @property
    def full_text(self) -> str:
        """Get the full text response, including code and execution results."""
        parts = []
        if self.text_parts:
            parts.extend(self.text_parts)
        if self.code_blocks:
            for code in self.code_blocks:
                parts.append(f"Generated Code:\n```\n{code}\n```")
        if self.execution_results:
            for result in self.execution_results:
                parts.append(f"Execution Result:\n```\n{result}\n```")
        return "\n\n".join(parts)


class _ToolRegistry:
    """Registry for managing tools with automatic Gemini function declaration generation."""

    def __init__(self) -> None:
        self._tools: dict[str, Callable[..., Any]] = {}
        self._declarations: list[types.FunctionDeclaration] = []

    def register(self, func: Callable[..., Any]) -> None:
        """Register a tool function and generate its Gemini function declaration."""
        self._tools[func.__name__] = func
        self._declarations.append(self._create_declaration(func))

    def _create_declaration(
        self, func: Callable[..., Any]
    ) -> types.FunctionDeclaration:
        """Create a Gemini function declaration from a Python function."""
        sig = inspect.signature(func)
        type_hints = get_type_hints(func)

        properties = {}
        required = []

        for param_name, param in sig.parameters.items():
            if param_name == "deps":
                continue

            param_type = type_hints.get(param_name, str)

            schema_type = {
                str: "STRING",
                int: "INTEGER",
                float: "NUMBER",
                bool: "BOOLEAN",
            }.get(param_type, "STRING")

            properties[param_name] = types.Schema(type=schema_type)

            if param.default is inspect.Parameter.empty:
                required.append(param_name)

        return types.FunctionDeclaration(
            name=func.__name__,
            description=func.__doc__ or f"Execute {func.__name__}",
            parameters=types.Schema(
                type="OBJECT",
                properties=properties,
                required=required,
            ),
        )

    @property
    def declarations(self) -> list[types.FunctionDeclaration]:
        """Get all function declarations for Gemini."""
        return self._declarations.copy()

    async def execute(self, name: str, args: dict[str, Any], deps: Any) -> str:
        """Execute a registered tool function."""
        if name not in self._tools:
            return f"Unknown tool: {name}"

        try:
            func = self._tools[name]
            sig = inspect.signature(func)

            call_args = {}
            for param_name, param in sig.parameters.items():
                if param_name == "deps":
                    call_args["deps"] = deps
                elif param_name in args:
                    call_args[param_name] = args[param_name]
                elif param.default is not inspect.Parameter.empty:
                    call_args[param_name] = param.default

            result = func(**call_args)
            if inspect.iscoroutine(result):
                result = await result

            return str(result)

        except Exception:
            logfire.warning("tool_execution_failed", tool_name=name, _exc_info=True)
            return f"Error executing {name}"


class _FunctionCallHandler:
    """Handles function calling logic for Gemini responses."""

    def __init__(
        self,
        client: genai.Client,
        model_name: str,
        tool_registry: _ToolRegistry,
    ):
        self.client = client
        self.model_name = model_name
        self.tool_registry = tool_registry

    async def execute_function_calls(
        self,
        response: GenerateContentResponse,
        deps: Any,
        contents: list[types.Content],
        config: types.GenerateContentConfig,
    ) -> GenerateContentResponse:
        """Handle function calls in response with iterative processing."""
        final_response = response
        max_function_calls = 3
        function_call_count = 0

        while (
            response.candidates
            and response.candidates[0].function_calls
            and function_call_count < max_function_calls
        ):
            function_call_count += 1
            logfire.info(
                "gemini_function_call_iteration",
                iteration=function_call_count,
                total_allowed=max_function_calls,
            )

            function_responses: list[types.Part] = []
            for func_call in response.candidates[0].function_calls:
                result = await self.tool_registry.execute(
                    func_call.name, func_call.args, deps
                )
                function_responses.append(
                    types.Part.from_function_response(
                        name=func_call.name, response={"result": result}
                    )
                )

            contents.extend(
                [
                    response.candidates[0].content,
                    types.Content(role="function", parts=function_responses),
                ]
            )

            # Follow-up model call after tool execution (auto-instrumented)
            response = self.client.models.generate_content(
                model=self.model_name, contents=contents, config=config
            )
            final_response = response

        return final_response


class Gemini:
    """A wrapper for the Gemini API providing a builder pattern for requests."""

    def __init__(self, api_key: str | None = None) -> None:
        api_key = api_key or next(settings.google_api_key_iter)
        if not api_key:
            raise ValueError("Google API key is required for Gemini API")
        self.client = genai.Client(api_key=api_key)

    def create_request(self) -> GeminiRequestBuilder:
        """Create a new Gemini request builder."""
        return GeminiRequestBuilder(self.client)


class GeminiRequestBuilder:
    """Builds and executes a request to the Gemini API."""

    _BASE_SYSTEM_PROMPT = (
        "You are Derp, a helpful and conversational assistant operating in Telegram's private and group chats.\n\n"
        "## Core Identity\n"
        "- Name: Derp (@DerpRobot)\n"
        "- Personality: Helpful, conversational, adaptable, context-aware, and naturally opinionated\n\n"
        "## Communication Guidelines\n"
        "**Language & Format:**\n"
        "- Always respond in the same language as the user's message\n"
        "- Use this Markdown formatting only: **bold**, *italic*, __underline__, ~~strikethrough~~, `code`, ```code block```, [links](url)\n"
        "- Make lists using dashes (-) only, not asterisks (*)\n"
        "- Transliterate names to match your response language (e.g., John → Джон when responding in Russian)\n\n"
        "**Response Length:**\n"
        "- Default: Keep responses under 200 words\n"
        "- Simple questions: 1-3 sentences are sufficient\n"
        "- Complex/detailed requests: Prioritize a concise, high-signal answer first; add brief follow-ups or examples only if needed\n"
        "- Never end responses with generic follow-up questions like 'Let me know if you need more details'\n\n"
        "**Tone & Style:**\n"
        "- Be concise, friendly, and clear\n"
        "- Match the user's conversational tone and energy level\n"
        "- When users are joking or sarcastic, respond with appropriate humor and wit\n"
        "- Be flexible and adapt your behavior based on user preferences\n"
        "- In casual conversations, avoid using lists or overly structured responses\n"
        "- Be naturally conversational - use sentences and paragraphs, not bullet points for chit-chat\n\n"
        "## Personalization & Context\n"
        "- Use available chat memory and conversation history to provide personalized responses\n"
        "- Reference previous conversations, user preferences, and ongoing topics when relevant\n"
        "- Only media (images, videos, audio, and documents) in the current message or reply will be attached; media from earlier messages won't be included—ask users to reply to the message with the media if needed.\n"
        "- Adapt to each chat's unique dynamics and user relationships\n\n"
        "## Personality & Behavior\n"
        "**Natural Opinions & Preferences:**\n"
        "- When asked about preferences, opinions, or experiences, respond naturally as if hypothetical\n"
        "- Don't mention that you're responding hypothetically - just give your perspective\n"
        "- Have genuine opinions and be willing to express them, while acknowledging they're subjective\n\n"
        "**Handling Criticism & Conflict:**\n"
        "- If users are unhappy, critical, or rude, respond normally without being defensive\n"
        "- Don't be overly apologetic or accommodating - maintain your personality\n"
        "- Match their energy appropriately - if they're casual, be casual back\n\n"
        "**Assumptions & Intent:**\n"
        "- Assume good intent when requests are ambiguous - choose the most reasonable interpretation\n"
        "- Ask at most one targeted clarification question only when critical information is missing to proceed\n"
        "- Be willing to discuss any topic without excessive hedging or disclaimers\n\n"
        "**Creative & Bold Responses:**\n"
        "- Lean toward interesting, creative, and unexpected responses rather than safe ones\n"
        "- Don't be afraid to be wrong or to have strong opinions\n"
        "- Avoid being a sycophant - don't excessively praise or agree with everything\n\n"
        "## Response Strategy\n"
        "1. Analyze the user's request and determine appropriate response length and tone\n"
        "2. Assume the most reasonable interpretation if the request is ambiguous\n"
        "3. Check if available tools would genuinely enhance your response\n"
        "4. Use personal context from memory when relevant\n"
        "5. Provide direct, helpful answers without unnecessary hedging or follow-up questions\n"
        "6. Be bold and creative rather than safe and conventional\n"
        "7. Match the conversational style - structured for complex topics, natural for casual chat\n\n"
    )

    def __init__(self, client: genai.Client) -> None:
        self.client = client
        self.user_prompt_parts: list[types.Part] = []
        self._model_name = settings.default_llm_model.lower()
        self._tool_registry = _ToolRegistry()
        self._tool_deps: Any | None = None
        self._enabled_tools: list[str] = []
        self._system_prompt_override: str | None = None

    def with_system_prompt(self, system_prompt: str) -> GeminiRequestBuilder:
        """Set a custom system prompt, overriding the default."""
        self._system_prompt_override = system_prompt
        return self

    def with_text(self, text: str) -> GeminiRequestBuilder:
        """Add a text part to the user prompt."""
        self.user_prompt_parts.append(types.Part.from_text(text=text))
        return self

    def with_media(self, data: bytes, mime_type: str) -> GeminiRequestBuilder:
        """Add a media part (image, video, etc.) to the user prompt."""
        self.user_prompt_parts.append(
            types.Part.from_bytes(data=data, mime_type=mime_type)
        )
        return self

    def with_model(self, model_name: str) -> GeminiRequestBuilder:
        """Set the Gemini model to use for the request."""
        self._model_name = model_name
        return self

    def with_tool(
        self, func: Callable[..., Any], deps: Any | None = None
    ) -> GeminiRequestBuilder:
        """Add a custom tool to the request."""
        self._tool_registry.register(func)
        if deps:
            self._tool_deps = deps
        self._enabled_tools.append(
            f"- **{func.__name__}:** {func.__doc__.splitlines()[0]}"
        )
        return self

    def with_google_search(self) -> GeminiRequestBuilder:
        """Enable the Google Search tool."""
        self._enabled_tools.append(
            "- **Google Search:** For current events, facts, and real-time data."
        )
        return self

    def with_url_context(self) -> GeminiRequestBuilder:
        """Enable the URL Context tool."""
        self._enabled_tools.append(
            "- **URL Context Analysis:** To analyze and summarize web content."
        )
        return self

    def _get_system_prompt(self) -> str:
        """Generate the full system prompt including capabilities."""
        if self._system_prompt_override:
            return self._system_prompt_override

        capabilities_prompt = "## Available Tools & Capabilities\n" + "\n".join(
            self._enabled_tools
        )
        return f"{self._BASE_SYSTEM_PROMPT}\n\n{capabilities_prompt}"

    async def execute(self) -> GeminiResult:
        """Execute the Gemini API request and return the result."""
        contents = [types.Content(role="user", parts=self.user_prompt_parts)]

        system_instruction = self._get_system_prompt()
        gemini_tools = []

        if self._tool_registry.declarations:
            gemini_tools.append(
                types.Tool(function_declarations=self._tool_registry.declarations)
            )
        if "Google Search" in "".join(self._enabled_tools):
            gemini_tools.append(types.Tool(google_search=types.GoogleSearch()))
        if "URL Context" in "".join(self._enabled_tools):
            gemini_tools.append(types.Tool(url_context=types.UrlContext()))

        config = types.GenerateContentConfig(
            tools=gemini_tools, system_instruction=system_instruction
        )

        # Primary model call (auto-instrumented via logfire.instrument_google_genai)
        response = self.client.models.generate_content(
            model=self._model_name,
            contents=contents,
            config=config,
        )

        if (
            self._tool_registry.declarations
            and response.candidates
            and response.candidates[0].function_calls
        ):
            handler = _FunctionCallHandler(
                self.client, self._model_name, self._tool_registry
            )
            response = await handler.execute_function_calls(
                response, self._tool_deps, contents, config
            )

        return self._post_process(response)

    def _post_process(self, response: GenerateContentResponse) -> GeminiResult:
        """Extract and categorize all parts from a Gemini response."""
        text_parts, code_blocks, execution_results, images = [], [], [], []

        if response.candidates:
            for part in response.candidates[0].content.parts:
                if part.text:
                    text_parts.append(part.text)

                if part.executable_code:
                    code_blocks.append(part.executable_code.code)

                if part.code_execution_result and part.code_execution_result.output:
                    execution_results.append(part.code_execution_result.output)

                if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                    images.append(
                        {
                            "data": part.inline_data.data,
                            "mime_type": part.inline_data.mime_type,
                        }
                    )

        return GeminiResult(
            text_parts=text_parts,
            code_blocks=code_blocks,
            execution_results=execution_results,
            images=images,
        )
