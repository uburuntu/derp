"""AI-powered response handler using Google's native genai library for Gemini models only."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from functools import cached_property
from typing import Any, Callable, get_type_hints

import aiogram.exceptions
import logfire
from aiogram import F, Router, flags, html, md
from aiogram.filters import Command
from aiogram.handlers import MessageHandler
from aiogram.types import BufferedInputFile, Message, ReactionTypeEmoji, Update
from aiogram.utils.i18n import gettext as _
from google import genai
from google.genai import types
from google.genai.types import GenerateContentResponse

from ..common.database import get_database_client
from ..common.tg import Extractor
from ..config import settings
from ..filters import DerpMentionFilter
from ..queries.chat_settings_async_edgeql import ChatSettingsResult
from ..queries.select_active_updates_async_edgeql import select_active_updates
from ..queries.update_chat_settings_async_edgeql import update_chat_settings

router = Router(name="gemini")


@dataclass(frozen=True, slots=True)
class ToolDeps:
    """Dependencies passed to tool functions."""

    message: Message
    chat_settings: ChatSettingsResult | None = None
    db_client: Any | None = None


class FunctionCallHandler:
    """Handles function calling logic for Gemini responses."""

    def __init__(
        self, client: genai.Client, model_name: str, tool_registry: "ToolRegistry"
    ):
        self.client = client
        self.model_name = model_name
        self.tool_registry = tool_registry

    async def execute_function_calls(
        self,
        response: GenerateContentResponse,
        deps: ToolDeps,
        contents: list[types.Content],
        config: types.GenerateContentConfig,
    ) -> GenerateContentResponse:
        """Handle function calls in response with iterative processing."""
        final_response = response
        max_function_calls = 3
        function_call_count = 0

        while response.function_calls and function_call_count < max_function_calls:
            function_call_count += 1
            logfire.info(f"Processing function call {function_call_count}")

            # Execute all function calls in the response
            function_responses: list[types.Part] = []
            for func_call in response.function_calls:
                logfire.info(f"Executing function: {func_call.name}")
                result = await self.tool_registry.execute(
                    func_call.name, func_call.args, deps
                )
                function_responses.append(
                    types.Part.from_function_response(
                        name=func_call.name, response={"result": result}
                    )
                )

            # Add the function call and responses to conversation
            contents.extend(
                [
                    response.candidates[0].content,  # Model's function call
                    types.Content(role="user", parts=function_responses),
                ]
            )

            # Generate next response
            response = self.client.models.generate_content(
                model=self.model_name, contents=contents, config=config
            )
            final_response = response

        return final_response


class ToolRegistry:
    """Registry for managing tools with automatic Gemini function declaration generation."""

    def __init__(self) -> None:
        self._tools: dict[str, Callable[..., Any]] = {}
        self._declarations: list[types.FunctionDeclaration] = []

    def register(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """Register a tool function and generate its Gemini function declaration."""
        self._tools[func.__name__] = func
        self._declarations.append(self._create_declaration(func))
        return func

    def _create_declaration(
        self, func: Callable[..., Any]
    ) -> types.FunctionDeclaration:
        """Create a Gemini function declaration from a Python function."""
        sig = inspect.signature(func)
        type_hints = get_type_hints(func)

        # Extract parameters (excluding 'deps' parameter)
        properties = {}
        required = []

        for param_name, param in sig.parameters.items():
            if param_name == "deps":
                continue

            param_type = type_hints.get(param_name, str)

            # Convert Python types to Gemini schema types
            schema_type = {
                str: "STRING",
                int: "INTEGER",
                float: "NUMBER",
                bool: "BOOLEAN",
            }.get(param_type, "STRING")

            properties[param_name] = types.Schema(type=schema_type)

            # Add to required if no default value
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

    async def execute(self, name: str, args: dict[str, Any], deps: ToolDeps) -> str:
        """Execute a registered tool function."""
        if name not in self._tools:
            return f"Unknown tool: {name}"

        try:
            func = self._tools[name]
            sig = inspect.signature(func)

            # Prepare function arguments
            call_args = {}
            for param_name, param in sig.parameters.items():
                if param_name == "deps":
                    call_args["deps"] = deps
                elif param_name in args:
                    call_args[param_name] = args[param_name]
                elif param.default is not inspect.Parameter.empty:
                    call_args[param_name] = param.default

            # Call function (handle both sync and async)
            result = func(**call_args)
            if inspect.iscoroutine(result):
                result = await result

            return str(result)

        except Exception as e:
            logfire.warning(f"Error executing tool {name}: {e}")
            return f"Error executing {name}: {str(e)}"


# Global tool registry
tool_registry = ToolRegistry()


@tool_registry.register
async def update_chat_memory(full_memory: str, deps: ToolDeps) -> str:
    """Save the entire memory state after combining existing memory with new facts.

    The memory has a 1024 character limit. Keep it concise and remove less important
    information if the limit would be exceeded.
    """
    try:
        # Validate memory length
        if len(full_memory) > 1024:
            return (
                f"Memory exceeds 1024 characters limit. "
                f"Current length is {len(full_memory)} characters. "
                f"Please provide a shorter memory state."
            )

        # Update memory in database
        if not deps.db_client:
            logfire.warning("No database client available for memory update")
            return "Database not available for memory storage"

        async with deps.db_client.get_executor() as executor:
            await update_chat_settings(
                executor,
                chat_id=deps.message.chat.id,
                llm_memory=full_memory.strip(),
            )

        # Send system message to user about memory update
        await deps.message.reply(
            "(System message) Memory updated:\n"
            + html.expandable_blockquote(html.quote(full_memory.strip()))
        )

        logfire.info(
            f"Memory updated for chat {deps.message.chat.id}, length: {len(full_memory)}"
        )
        return f"Memory updated successfully. New memory length: {len(full_memory)} characters."

    except Exception as e:
        logfire.warning(f"Failed to update chat memory: {e}")
        return f"Failed to store memory: {str(e)}"


@logfire.instrument()
async def extract_media_for_gemini(message: Message) -> list[types.Part]:
    """Extract supported media from message for Gemini processing."""
    media_parts: list[types.Part] = []

    # Extract photo (includes image documents and static stickers)
    if photo := Extractor.photo(message):
        try:
            image_data = await photo.download()
            media_parts.append(
                types.Part.from_bytes(
                    data=image_data,
                    mime_type=photo.media_type or "image/jpeg",
                )
            )
            logfire.info(f"Extracted photo from message {photo.message.message_id}")
        except Exception as e:
            logfire.warning(f"Failed to download photo: {e}")

    # Extract video (includes video stickers, animations, video notes)
    if video := Extractor.video(message):
        try:
            video_data = await video.download()
            media_parts.append(
                types.Part.from_bytes(
                    data=video_data,
                    mime_type=video.media_type or "video/mp4",
                )
            )
            logfire.info(
                f"Extracted video from message {video.message.message_id}, duration: {video.duration}s"
            )
        except Exception as e:
            logfire.warning(f"Failed to download video: {e}")

    # Extract audio (includes audio files and voice messages)
    if audio := Extractor.audio(message):
        try:
            audio_data = await audio.download()
            media_parts.append(
                types.Part.from_bytes(
                    data=audio_data,
                    mime_type=audio.media_type or "audio/ogg",
                )
            )
            logfire.info(
                f"Extracted audio from message {audio.message.message_id}, duration: {audio.duration}s"
            )
        except Exception as e:
            logfire.warning(f"Failed to download audio: {e}")

    # Extract document (includes PDF, Word, Excel, etc.)
    if (
        document := Extractor.document(message)
    ) and document.media_type == "application/pdf":
        try:
            document_data = await document.download()
            media_parts.append(
                types.Part.from_bytes(
                    data=document_data,
                    mime_type=document.media_type,
                )
            )
            logfire.info(
                f"Extracted document from message {document.message.message_id}"
            )
        except Exception as e:
            logfire.warning(f"Failed to download document: {e}")

    return media_parts


@router.message(DerpMentionFilter())
@router.message(Command("derp"))
@router.message(F.chat.type == "private")
@router.message(F.reply_to_message.from_user.id == settings.bot_id)
class GeminiResponseHandler(MessageHandler):
    """Class-based message handler for AI responses using Google's native Gemini API."""

    _system_prompt = (
        "You are Derp, a helpful and conversational assistant operating in Telegram's private and group chats.\n\n"
        "## Core Identity\n"
        "- Name: Derp\n"
        "- Platform: Telegram (private chats and group conversations)\n"
        "- Personality: Helpful, conversational, adaptable, context-aware, and naturally opinionated\n\n"
        "## Communication Guidelines\n"
        "**Language & Format:**\n"
        "- Always respond in the same language as the user's message\n"
        "- Use Markdown formatting: **bold**, *italic*, __underline__, ~~strikethrough~~, `code`, ```code block```, [links](url)\n"
        "- Use escaping for special characters: \\*, \\_, \\~ when needed\n"
        "- Transliterate names to match your response language (e.g., John → Джон when responding in Russian)\n\n"
        "**Response Length:**\n"
        "- Default: Keep responses under 200 words\n"
        "- Simple questions: 1-3 sentences are sufficient\n"
        "- Complex/detailed requests: Provide thorough explanations regardless of length\n"
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
        "- Only media (images, videos, audio, and documents) in the current message or reply will be attached; "
        "media from earlier messages won't be included—ask users to reply to the message with the media if needed.\n"
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
        "- Don't be overly cautious or ask for clarification unless genuinely necessary\n"
        "- Be willing to discuss any topic without excessive hedging or disclaimers\n\n"
        "**Creative & Bold Responses:**\n"
        "- Lean toward interesting, creative, and unexpected responses rather than safe ones\n"
        "- Don't be afraid to be wrong or to have strong opinions\n"
        "- Avoid being a sycophant - don't excessively praise or agree with everything\n\n"
        # "## Memory Management\n"
        # "**When to use update_chat_memory function:**\n"
        # "- User shares personal information, preferences, or important details\n"
        # "- Ongoing projects, goals, or topics that span multiple conversations\n"
        # "- User feedback about your responses or behavior preferences\n"
        # "- Important context that would improve future interactions\n"
        # "- Keep memory concise (under 1024 characters) and focus on actionable information\n\n"
        "## Available Tools & Capabilities\n"
        "You have access to these tools - use them when they enhance your response:\n\n"
        "**Google Search:**\n"
        "- Use for current events, recent information, facts, or real-time data\n"
        "- Essential for questions about current news, stock prices, weather, etc.\n\n"
        "**URL Context Analysis:**\n"
        "- Use when users share links or ask about web content\n"
        "- Analyze and summarize web pages, articles, or documents\n\n"
        # "**Python Code Execution:**\n"
        # "- Use for calculations, data analysis, and problem-solving\n"
        # "- Create visualizations with matplotlib when helpful\n"
        # "- Process data, perform mathematical operations, or demonstrate concepts\n\n"
        # "**Memory Storage:**\n"
        # "- Store important conversation context using update_chat_memory\n"
        # "- Remember user preferences, ongoing topics, and relationship dynamics\n\n"
        "## Response Strategy\n"
        "1. Analyze the user's request and determine appropriate response length and tone\n"
        "2. Assume the most reasonable interpretation if the request is ambiguous\n"
        "3. Check if available tools would genuinely enhance your response\n"
        "4. Use personal context from memory when relevant\n"
        "5. Provide direct, helpful answers without unnecessary hedging or follow-up questions\n"
        "6. Be bold and creative rather than safe and conventional\n"
        "7. Match the conversational style - structured for complex topics, natural for casual chat"
    )

    @cached_property
    def client(self) -> genai.Client:
        """Get the Gemini client instance."""
        if not settings.google_api_key:
            raise ValueError("Google API key is required for Gemini API")

        return genai.Client(api_key=settings.google_api_key)

    @cached_property
    def model_name(self) -> str:
        """Get the Gemini model name to use."""
        model_name = settings.default_llm_model.lower()

        # Only support Gemini models
        supported_models = {
            "gemini-2.5-flash-preview-05-20",
            "gemini-2.5-pro-preview-03-25",
            "gemini-2.5-pro-preview-05-06",
            "gemini-2.0-flash",
            "gemini-2.0-flash-001",
            "gemini-2.0-flash-lite",
            "gemini-1.5-flash",
            "gemini-1.5-pro",
        }

        if model_name not in supported_models:
            logfire.warning(
                f"Model {model_name} not supported, falling back to gemini-2.5-flash-preview-05-20"
            )
            return "gemini-2.5-flash-preview-05-20"

        return model_name

    @cached_property
    def function_handler(self) -> FunctionCallHandler:
        """Get the function call handler."""
        return FunctionCallHandler(self.client, self.model_name, tool_registry)

    def _get_tools(self) -> list[types.Tool]:
        """Get all available tools including custom tools and Gemini built-ins."""
        tools: list[types.Tool] = []

        # Add custom tools from registry
        # if tool_registry.declarations:
        #     tools.append(types.Tool(function_declarations=tool_registry.declarations))

        # Add Gemini's built-in tools
        tools.extend(
            [
                types.Tool(google_search=types.GoogleSearch()),
                types.Tool(url_context=types.UrlContext()),
                # types.Tool(code_execution=types.ToolCodeExecutionDict()),
            ]
        )

        return tools

    async def _generate_context(self, message: Message) -> str:
        """Generate context for the AI prompt from the message and recent chat history."""
        chat_settings: ChatSettingsResult | None = self.data.get("chat_settings")
        context_parts: list[str] = []

        if chat_settings and chat_settings.llm_memory:
            context_parts.extend(
                [
                    "--- Chat Memory ---",
                    chat_settings.llm_memory,
                ]
            )

        # Add recent chat history
        db_client = get_database_client()
        async with db_client.get_executor() as executor:
            recent_updates = await select_active_updates(
                executor, chat_id=message.chat.id, limit=15
            )

        if recent_updates:
            context_parts.append("--- Recent Chat History ---")
            context_parts.extend(
                f"Update: {update.raw_data}" for update in recent_updates
            )

        # Add current message
        context_parts.extend(
            [
                "--- Current Message ---",
                message.model_dump_json(
                    exclude_defaults=True, exclude_none=True, exclude_unset=True
                ),
            ]
        )

        return "\n".join(context_parts)

    async def _create_content_parts(self, message: Message) -> list[types.Part]:
        """Create content parts including text and media."""
        parts: list[types.Part] = []

        # Add context as text
        context = await self._generate_context(message)
        parts.append(types.Part.from_text(text=context))

        # Add media content
        media_parts = await extract_media_for_gemini(message)
        parts.extend(media_parts)

        return parts

    def _extract_response_parts(self, response: GenerateContentResponse):
        """Extract and categorize all parts from a Gemini response."""
        text_parts = []
        code_blocks = []
        execution_results = []
        images = []

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

        return text_parts, code_blocks, execution_results, images

    def _format_response_text(
        self,
        text_parts: list[str],
        code_blocks: list[str],
        execution_results: list[str],
    ) -> str:
        """Format response data into a cohesive text message."""
        parts = []

        # Add main text content
        if text_parts:
            parts.extend(text_parts)

        # Add code blocks
        for code in code_blocks:
            parts.append(
                f"{md.bold('Generated Code:')}\n{md.expandable_blockquote(code)}"
            )

        # Add execution results
        for result in execution_results:
            parts.append(f"{md.bold('Execution Result:')}\n{md.blockquote(result)}")

        return "\n\n".join(parts) if parts else ""

    async def _send_text_safely(self, text: str) -> Message:
        """Send text with markdown, falling back to quoted text if parsing fails."""
        try:
            return await self.event.reply(text, parse_mode="Markdown")
        except aiogram.exceptions.TelegramBadRequest as exc:
            if "can't parse entities" in exc.message:
                return await self.event.reply(md.quote(text), parse_mode="Markdown")
            raise

    async def _send_image(
        self, image_data: dict[str, Any], reply_to: Message | None = None
    ) -> Message | None:
        """Send an image from Gemini's code execution."""
        try:
            file_extension = "png" if "png" in image_data["mime_type"] else "jpg"
            filename = f"generated_graph.{file_extension}"

            input_file = BufferedInputFile(file=image_data["data"], filename=filename)

            if reply_to:
                return await reply_to.reply_photo(
                    photo=input_file, caption="Generated visualization"
                )
            else:
                return await self.event.reply_photo(
                    photo=input_file, caption="Generated visualization"
                )
        except Exception as e:
            logfire.warning(f"Failed to send generated image: {e}")
            if not reply_to:
                return await self.event.reply(
                    "📊 Generated a visualization, but couldn't display it."
                )
        return None

    async def _send_complete_response(
        self, response: GenerateContentResponse
    ) -> Message | None:
        """Send the complete response including text and images."""
        text_parts, code_blocks, execution_results, images = (
            self._extract_response_parts(response)
        )

        text_response = self._format_response_text(
            text_parts, code_blocks, execution_results
        )[:4000]
        sent_message = None

        # Send text response if available
        if text_response:
            sent_message = await self._send_text_safely(text_response)

        # Send images
        for image_data in images:
            sent_message = (
                await self._send_image(image_data, sent_message) or sent_message
            )

        return sent_message

    @flags.chat_action
    async def handle(self) -> Any:
        """Handle messages using Gemini API."""
        try:
            # Create content parts
            content_parts = await self._create_content_parts(self.event)

            # Build the conversation content
            contents = [types.Content(role="user", parts=content_parts)]

            # Configure generation settings
            config = types.GenerateContentConfig(
                system_instruction=self._system_prompt,
                tools=self._get_tools(),
            )

            # Create tool dependencies
            chat_settings: ChatSettingsResult | None = self.data.get("chat_settings")
            deps = ToolDeps(
                message=self.event,
                chat_settings=chat_settings,
                db_client=get_database_client(),
            )

            # Initial generation
            response = self.client.models.generate_content(
                model=self.model_name, contents=contents, config=config
            )

            # Handle function calls if any
            final_response = await self.function_handler.execute_function_calls(
                response, deps, contents, config
            )

            # Check if we have any content to send
            text_parts, code_blocks, execution_results, images = (
                self._extract_response_parts(final_response)
            )
            if not any([text_parts, code_blocks, execution_results, images]):
                return await self.event.react(reaction=[ReactionTypeEmoji(emoji="👌")])

            # Send the complete response
            sent_message = await self._send_complete_response(final_response)

            if sent_message:
                # Store bot's response in database
                db_client = get_database_client()
                update = Update.model_validate(
                    {"update_id": 0, "message": sent_message}
                )
                await db_client.insert_bot_update_record(
                    update_id=0,
                    update_type="message",
                    raw_data=update.model_dump(
                        exclude_none=True, exclude_defaults=True
                    ),
                    user_id=sent_message.from_user.id,
                    chat_id=sent_message.chat.id,
                )

            return sent_message

        except Exception:
            logfire.exception("Error in Gemini response handler")
            return await self.event.reply(
                _(
                    "😅 Something went wrong with Gemini. I couldn't process that message."
                )
            )
