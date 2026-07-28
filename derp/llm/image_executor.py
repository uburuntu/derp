"""Pydantic AI adapter for provider-neutral image execution."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

from pydantic_ai import AgentRunResult, BinaryContent, BinaryImage, UserContent

from derp.execution import (
    ExecutionPlan,
    Outcome,
    Rejected,
    RejectionReason,
    Succeeded,
)
from derp.features import (
    ImageGenerateRequest,
    ImageOutput,
    MediaContent,
    PreparedImageEditRequest,
)
from derp.inference.report import reports_from_messages
from derp.llm.agents import create_image_agent
from derp.media import MediaFamily


class ImageAgent(Protocol):
    """Narrow agent surface consumed by the Google image adapter."""

    async def run(
        self,
        user_prompt: str | Sequence[UserContent],
    ) -> AgentRunResult[BinaryImage | str]: ...


type ImageAgentFactory = Callable[[ExecutionPlan], ImageAgent]


class PydanticAIImageExecutor:
    """Translate Pydantic AI native image responses into domain outcomes."""

    def __init__(
        self,
        agent_factory: ImageAgentFactory = create_image_agent,
    ) -> None:
        self._agent_factory = agent_factory

    async def generate(
        self,
        plan: ExecutionPlan,
        request: ImageGenerateRequest,
    ) -> Outcome[ImageOutput]:
        """Generate images using the exact catalog-resolved execution plan."""
        prompt = request.prompt
        if request.style:
            prompt = f"{prompt}\n\nStyle: {request.style}"
        result = await self._agent_factory(plan).run(prompt)
        return self._outcome(result, plan)

    async def edit(
        self,
        plan: ExecutionPlan,
        request: PreparedImageEditRequest,
    ) -> Outcome[ImageOutput]:
        """Edit one already-bounded source image using the exact plan."""
        result = await self._agent_factory(plan).run(
            [
                BinaryContent(
                    data=request.source.data,
                    media_type=request.source.mime_type,
                ),
                f"Edit this image: {request.prompt}",
            ]
        )
        return self._outcome(result, plan)

    @staticmethod
    def _outcome(
        result: AgentRunResult[BinaryImage | str],
        plan: ExecutionPlan,
    ) -> Outcome[ImageOutput]:
        reports = reports_from_messages(
            result.new_messages(),
            requested_model=plan.model.provider_model_id,
        )
        images = result.response.images
        if not images and isinstance(result.output, BinaryImage):
            images = [result.output]
        if images:
            return Succeeded(
                ImageOutput(
                    images=tuple(
                        MediaContent(
                            family=MediaFamily.IMAGE,
                            mime_type=image.media_type,
                            data=image.data,
                        )
                        for image in images
                    ),
                    reports=reports,
                )
            )
        if isinstance(result.output, str):
            return Rejected(RejectionReason.POLICY)
        return Rejected(RejectionReason.UNUSABLE_OUTPUT)


__all__ = [
    "ImageAgent",
    "ImageAgentFactory",
    "PydanticAIImageExecutor",
]
