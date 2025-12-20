"""Image generation and editing tools for Pydantic-AI agents.

These tools use Gemini's Nano Banana (gemini-2.5-flash-image) model
for native image generation. Images are sent directly to the chat.

Reference: https://ai.google.dev/gemini-api/docs/nanobanana
"""

from __future__ import annotations

import logfire
from pydantic_ai import BinaryContent, BinaryImage, RunContext

from derp.common.extractor import Extractor
from derp.common.sender import MessageSender
from derp.llm.agents import create_image_agent
from derp.llm.deps import AgentDeps
from derp.tools.wrapper import credit_aware_tool


@credit_aware_tool("image_generate")
async def generate_image(
    ctx: RunContext[AgentDeps],
    prompt: str,
    *,
    style: str | None = None,
) -> str:
    """Generate an image based on the given prompt.

    Use this tool when the user asks you to create, generate, draw, or
    make an image. The image will be sent directly to the chat.

    Args:
        ctx: The run context with agent dependencies.
        prompt: A detailed description of the image to generate.
        style: Optional style hint (realistic, cartoon, artistic, etc.)

    Returns:
        A message confirming the image was generated or an error.
    """
    deps = ctx.deps

    # Build the full prompt
    full_prompt = prompt
    if style:
        full_prompt = f"{prompt}, {style} style"

    logfire.info(
        "image_generation_started",
        prompt_length=len(prompt),
        has_style=style is not None,
        chat_id=deps.chat_id,
    )

    try:
        # Create image agent and generate
        agent = create_image_agent()
        result = await agent.run(full_prompt)
        output = result.output

        # Handle the output
        if isinstance(output, BinaryImage):
            sender = MessageSender.from_message(deps.message)
            await sender.compose().text(f"🎨 {prompt}").image(output).reply()

            logfire.info(
                "image_generated_and_sent",
                chat_id=deps.chat_id,
                prompt_length=len(prompt),
            )

            return "I've generated and sent the image to the chat."

        elif isinstance(output, str):
            # Model returned text instead of image (refusal or error)
            logfire.warning(
                "image_generation_text_response",
                response=output[:200],
                chat_id=deps.chat_id,
            )
            return f"I couldn't generate that image: {output}"

        else:
            logfire.warning(
                "image_generation_unexpected_output",
                output_type=type(output).__name__,
            )
            return "Something unexpected happened during image generation."

    except Exception as exc:
        logfire.exception("image_generation_failed", chat_id=deps.chat_id)
        return f"Image generation failed: {exc!s}"


@credit_aware_tool("image_edit")
async def edit_image(
    ctx: RunContext[AgentDeps],
    edit_prompt: str,
) -> str:
    """Edit an image that the user has sent.

    Use this tool when the user sends an image and asks you to modify,
    edit, or change it in some way. The edited image will be sent to the chat.

    Args:
        ctx: The run context with agent dependencies.
        edit_prompt: Description of the edits to make to the image.

    Returns:
        A message confirming the edit was applied or an error.
    """
    deps = ctx.deps
    message = deps.message

    logfire.info(
        "image_edit_started",
        prompt_length=len(edit_prompt),
        chat_id=deps.chat_id,
    )

    # Extract the image from the message or replied message
    photo = await Extractor.photo(message)
    if not photo and message.reply_to_message:
        photo = await Extractor.photo(message.reply_to_message)

    if not photo:
        return (
            "I don't see an image to edit. Please send or reply to an image "
            "and tell me what changes you'd like."
        )

    try:
        # Download the image
        image_data = await photo.download()

        # Create image agent and run with the image + edit prompt
        agent = create_image_agent()
        result = await agent.run(
            [
                BinaryContent(
                    data=image_data,
                    media_type=photo.media_type or "image/jpeg",
                ),
                f"Edit this image: {edit_prompt}",
            ]
        )
        output = result.output

        # Handle the output
        if isinstance(output, BinaryImage):
            sender = MessageSender.from_message(message)
            await sender.compose().text(f"✏️ {edit_prompt}").image(output).reply()

            logfire.info(
                "image_edited_and_sent",
                chat_id=deps.chat_id,
                prompt_length=len(edit_prompt),
            )

            return "I've edited and sent the image to the chat."

        elif isinstance(output, str):
            logfire.warning(
                "image_edit_text_response",
                response=output[:200],
                chat_id=deps.chat_id,
            )
            return f"I couldn't edit that image: {output}"

        else:
            logfire.warning(
                "image_edit_unexpected_output",
                output_type=type(output).__name__,
            )
            return "Something unexpected happened during image editing."

    except Exception as exc:
        logfire.exception("image_edit_failed", chat_id=deps.chat_id)
        return f"Image editing failed: {exc!s}"
