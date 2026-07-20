"""Quote-first orchestration for image tools awaiting Telegram approval."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic_ai import ModelMessage, ModelResponse, ToolCallPart
from sqlalchemy import Select, select

from derp.approvals.service import DeferredToolApprovalService
from derp.approvals.types import DeferredToolHandle
from derp.catalog import GoogleModelKey, get_google_model
from derp.db import DatabaseManager
from derp.delivery import DeliveryTarget
from derp.execution import ExecutionPlan, Feature, plan_execution
from derp.features import (
    ImageEditRequest,
    ImageGenerateRequest,
    ImageInvocation,
    ImageOperationCoordinator,
    ImageRequest,
)
from derp.history.core import DEFAULT_TOKEN_ESTIMATOR
from derp.history.persistence import load_source_snapshot
from derp.history.snapshot import AttachmentMediaType, AttachmentSnapshot
from derp.media import MediaMetadata, MediaReference
from derp.media.types import normalize_mime_type
from derp.models import Message
from derp.operations import FinishingChatQuoteInput, OperationId, Quote

GENERATE_IMAGE_TOOL = "generate_image"
EDIT_IMAGE_TOOL = "edit_image"
DEFERRED_IMAGE_TOOLS = frozenset({GENERATE_IMAGE_TOOL, EDIT_IMAGE_TOOL})
FINISHING_INPUT_OVERHEAD_TOKENS = 2_048


class DeferredImageToolError(ValueError):
    """A deferred image call cannot be mapped to a durable operation."""


class MissingImageSourceError(DeferredImageToolError):
    """An edit call has no restart-safe Telegram image reference."""


@dataclass(frozen=True, slots=True)
class ImageToolRunContext:
    """Stable Telegram, ownership, and delivery facts for one image tool call."""

    requester_id: uuid.UUID
    requester_telegram_id: int
    chat_id: uuid.UUID
    chat_telegram_id: int
    message_id: int
    thread_id: int | None
    business_connection_id: str | None = field(default=None, repr=False)
    source: MediaReference | None = field(default=None, repr=False)
    allow_personal_once: bool = field(default=False, repr=False)
    finishing_quote_input: FinishingChatQuoteInput | None = None

    def __post_init__(self) -> None:
        for name in ("requester_id", "chat_id"):
            if not isinstance(getattr(self, name), uuid.UUID):
                raise TypeError(f"{name} must be a UUID")
        if self.requester_telegram_id <= 0:
            raise ValueError("requester_telegram_id must be positive")
        if self.chat_telegram_id == 0:
            raise ValueError("chat_telegram_id must not be zero")
        if self.message_id <= 0:
            raise ValueError("message_id must be positive")
        if self.thread_id is not None and self.thread_id <= 0:
            raise ValueError("thread_id must be positive")
        if self.business_connection_id is not None and not (
            self.business_connection_id.strip()
        ):
            raise ValueError("business_connection_id must not be blank")
        if self.source is not None and not isinstance(self.source, MediaReference):
            raise TypeError("source must be a MediaReference")
        if not isinstance(self.allow_personal_once, bool):
            raise TypeError("allow_personal_once must be a bool")
        if self.finishing_quote_input is not None and not isinstance(
            self.finishing_quote_input,
            FinishingChatQuoteInput,
        ):
            raise TypeError("finishing_quote_input must be a FinishingChatQuoteInput")


@dataclass(frozen=True, slots=True)
class DeferredImageCall:
    """A Pydantic-AI call converted to the validated feature request."""

    tool_call: ToolCallPart = field(repr=False)
    feature: Feature
    request: ImageRequest = field(repr=False)

    @classmethod
    def parse(
        cls,
        tool_call: ToolCallPart,
        *,
        source: MediaReference | None,
    ) -> DeferredImageCall:
        if not isinstance(tool_call, ToolCallPart):
            raise TypeError("tool_call must be a ToolCallPart")
        try:
            arguments = tool_call.args_as_dict(raise_if_invalid=True)
        except Exception:
            raise DeferredImageToolError("image tool arguments are invalid") from None

        if tool_call.tool_name == GENERATE_IMAGE_TOOL:
            _require_argument_keys(
                arguments,
                required=frozenset({"prompt"}),
                optional=frozenset({"style"}),
            )
            prompt = arguments["prompt"]
            style = arguments.get("style")
            if not isinstance(prompt, str) or (
                style is not None and not isinstance(style, str)
            ):
                raise DeferredImageToolError("image generation arguments are invalid")
            try:
                request: ImageRequest = ImageGenerateRequest(prompt, style=style)
            except TypeError, ValueError:
                raise DeferredImageToolError(
                    "image generation arguments are invalid"
                ) from None
            feature = Feature.IMAGE_GENERATE
        elif tool_call.tool_name == EDIT_IMAGE_TOOL:
            _require_argument_keys(
                arguments,
                required=frozenset({"edit_prompt"}),
                optional=frozenset(),
            )
            prompt = arguments["edit_prompt"]
            if not isinstance(prompt, str):
                raise DeferredImageToolError("image edit arguments are invalid")
            if source is None:
                raise MissingImageSourceError(
                    "image editing requires an attached or replied-to image"
                )
            try:
                request = ImageEditRequest(prompt, source)
            except TypeError, ValueError:
                raise DeferredImageToolError(
                    "image edit arguments are invalid"
                ) from None
            feature = Feature.IMAGE_EDIT
        else:
            raise DeferredImageToolError("unsupported deferred image tool")

        return cls(tool_call=tool_call, feature=feature, request=request)

    def invocation(self, context: ImageToolRunContext) -> ImageInvocation:
        operation_id = OperationId.for_tool(
            feature=self.feature,
            chat_id=context.chat_telegram_id,
            message_id=context.message_id,
            tool_call_id=self.tool_call.tool_call_id,
        )
        return ImageInvocation(
            operation_id=operation_id,
            request_key=f"telegram:tool:{operation_id}",
            requester_id=context.requester_id,
            chat_id=context.chat_id,
            thread_id=context.thread_id,
            target=DeliveryTarget(
                chat_id=context.chat_telegram_id,
                thread_id=context.thread_id,
                reply_to_message_id=context.message_id,
                business_connection_id=context.business_connection_id,
            ),
            input_tokens=DEFAULT_TOKEN_ESTIMATOR.estimate_text(self.request.prompt),
        )


@dataclass(frozen=True, slots=True)
class PreparedImageToolApproval:
    """Exact quote and opaque approval handle ready for presentation."""

    call: DeferredImageCall
    quote: Quote
    handle: DeferredToolHandle


class ImageToolApprovalCoordinator:
    """Persist an exact image quote before exposing approval controls."""

    def __init__(
        self,
        image_operations: ImageOperationCoordinator,
        approvals: DeferredToolApprovalService,
    ) -> None:
        self._image_operations = image_operations
        self._approvals = approvals

    async def prepare(
        self,
        *,
        context: ImageToolRunContext,
        tool_call: ToolCallPart,
        original_history: list[ModelMessage] | tuple[ModelMessage, ...],
    ) -> PreparedImageToolApproval:
        """Quote and persist one framework-validated tool call without executing it."""
        call = DeferredImageCall.parse(tool_call, source=context.source)
        invocation = call.invocation(context)
        finishing_plan, finishing_quote_input = finishing_quote_from_history(
            original_history
        )
        quote = await self._image_operations.ensure_quote(
            invocation,
            plan_execution(call.feature, GoogleModelKey.IMAGE),
            call.request,
            finishing_plan=finishing_plan,
            finishing_quote_input=finishing_quote_input,
        )
        handle = await self._approvals.create_request(
            operation_id=invocation.operation_id,
            quote_id=quote.id,
            message_id=context.message_id,
            tool_call=tool_call,
            original_history=original_history,
        )
        return PreparedImageToolApproval(call, quote, handle)


def finishing_quote_from_history(
    history: Sequence[ModelMessage],
) -> tuple[ExecutionPlan, FinishingChatQuoteInput]:
    """Build the deterministic post-approval allowance from durable history."""
    supported = (GoogleModelKey.CHAT_ECONOMY, GoogleModelKey.CHAT_STANDARD)
    model_key: GoogleModelKey | None = None
    for message in reversed(history):
        if not isinstance(message, ModelResponse) or message.model_name is None:
            continue
        for candidate in supported:
            if message.model_name == get_google_model(candidate).provider_model_id:
                model_key = candidate
                break
        if model_key is not None:
            break
    if model_key is None:
        raise DeferredImageToolError(
            "deferred chat model cannot be reconstructed from trusted history"
        )

    input_tokens = FINISHING_INPUT_OVERHEAD_TOKENS + sum(
        DEFAULT_TOKEN_ESTIMATOR.estimate_text(repr(message)) + 4 for message in history
    )
    return (
        plan_execution(Feature.CHAT, model_key),
        FinishingChatQuoteInput(model_key, input_tokens),
    )


async def load_persisted_image_source(
    db: DatabaseManager,
    *,
    chat_id: uuid.UUID,
    message_id: int,
) -> MediaReference | None:
    """Recover current-or-replied image metadata without downloading media."""
    now = datetime.now(UTC)
    async with db.read_session() as session:
        current = await session.scalar(
            _live_message_query(chat_id=chat_id, message_id=message_id, now=now)
        )
        if current is None:
            return None
        if source := _image_reference_from_message(current):
            return source
        if current.reply_to_message_id is None:
            return None
        replied = await session.scalar(
            _live_message_query(
                chat_id=chat_id,
                message_id=current.reply_to_message_id,
                now=now,
            )
        )
        return _image_reference_from_message(replied) if replied else None


def _live_message_query(
    *,
    chat_id: uuid.UUID,
    message_id: int,
    now: datetime,
) -> Select[tuple[Message]]:
    return select(Message).where(
        Message.chat_id == chat_id,
        Message.telegram_message_id == message_id,
        Message.deleted_at.is_(None),
        Message.privacy_deleted_at.is_(None),
        Message.retention_expires_at > now,
    )


def _image_reference_from_message(message: Message) -> MediaReference | None:
    try:
        snapshot = load_source_snapshot(message.source_snapshot)
    except Exception:
        return None
    for attachment in snapshot.attachments:
        if reference := _image_reference_from_attachment(attachment):
            return reference
    return None


def _image_reference_from_attachment(
    attachment: AttachmentSnapshot,
) -> MediaReference | None:
    if attachment.media_type is AttachmentMediaType.PHOTO:
        mime_type = "image/jpeg"
    elif attachment.media_type is AttachmentMediaType.DOCUMENT:
        if attachment.mime_type is None:
            return None
        try:
            mime_type = normalize_mime_type(attachment.mime_type)
        except ValueError:
            return None
        if not mime_type.startswith("image/"):
            return None
    else:
        return None
    return MediaReference(
        file_id=attachment.file_id,
        file_unique_id=attachment.file_unique_id,
        metadata=MediaMetadata(
            mime_type=mime_type,
            file_size=attachment.size,
            file_name=attachment.filename,
            width=attachment.width,
            height=attachment.height,
        ),
    )


def _require_argument_keys(
    arguments: dict[str, object],
    *,
    required: frozenset[str],
    optional: frozenset[str],
) -> None:
    keys = frozenset(arguments)
    if not required <= keys or keys - required - optional:
        raise DeferredImageToolError("image tool argument shape is invalid")


__all__ = [
    "DEFERRED_IMAGE_TOOLS",
    "EDIT_IMAGE_TOOL",
    "FINISHING_INPUT_OVERHEAD_TOKENS",
    "GENERATE_IMAGE_TOOL",
    "DeferredImageCall",
    "DeferredImageToolError",
    "ImageToolApprovalCoordinator",
    "ImageToolRunContext",
    "MissingImageSourceError",
    "PreparedImageToolApproval",
    "finishing_quote_from_history",
    "load_persisted_image_source",
]
