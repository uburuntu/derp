"""Provider-neutral feature execution boundaries."""

from typing import TYPE_CHECKING, Any

from derp.features.image import (
    MAX_IMAGE_OUTPUT_BYTES,
    V1_IMAGE_OUTPUT_COUNT,
    ImageEditRequest,
    ImageExecutionPolicy,
    ImageFeatureService,
    ImageGenerateRequest,
    ImageOutput,
    ImageProviderExecutor,
    ImageProviderRouter,
    ImageSourceLoader,
    PreparedImageEditRequest,
)
from derp.features.inline_chat import (
    DEFAULT_INLINE_DAILY_REQUESTS,
    FREE_INLINE_CHAT_PLAN,
    INLINE_CHAT_PLAN,
    MAX_INLINE_OUTPUT_CHARS,
    MAX_INLINE_OUTPUT_TOKENS,
    MAX_INLINE_QUERY_CHARS,
    InlineChatCompleted,
    InlineChatExhausted,
    InlineChatFailed,
    InlineChatFailureReason,
    InlineChatFeatureService,
    InlineChatInvalid,
    InlineChatInvocation,
    InlineChatOutcome,
    InlineChatPolicy,
    InlineProviderExecution,
)
from derp.features.think import (
    MAX_THINK_OUTPUT_CHARS,
    MAX_THINK_OUTPUT_TOKENS,
    MAX_THINK_PROBLEM_CHARS,
    PreparedThinkRequest,
    ThinkExecutionPolicy,
    ThinkFeatureService,
    ThinkProviderExecutor,
    ThinkRequest,
)
from derp.features.tts import (
    MAX_TTS_OUTPUT_BYTES,
    MAX_TTS_OUTPUT_SECONDS,
    MAX_TTS_TEXT_CHARS,
    TtsExecutionPolicy,
    TtsFeatureService,
    TtsProviderExecutor,
    TtsProviderOutput,
    TtsRequest,
)
from derp.features.types import MediaContent, TextOutput

if TYPE_CHECKING:
    from derp.features.image_operation import (
        ImageAwaitingFunding,
        ImageDelivered,
        ImageDeliveryUncertain,
        ImageInProgress,
        ImageInvocation,
        ImageNotCharged,
        ImageNotChargedReason,
        ImageOperationCoordinator,
        ImageOperationOutcome,
        ImageRefunded,
        ImageRequest,
    )

_IMAGE_OPERATION_EXPORTS = frozenset(
    {
        "ImageAwaitingFunding",
        "ImageDelivered",
        "ImageDeliveryUncertain",
        "ImageInProgress",
        "ImageInvocation",
        "ImageNotCharged",
        "ImageNotChargedReason",
        "ImageOperationCoordinator",
        "ImageOperationOutcome",
        "ImageRefunded",
        "ImageRequest",
    }
)

__all__ = [
    "DEFAULT_INLINE_DAILY_REQUESTS",
    "FREE_INLINE_CHAT_PLAN",
    "INLINE_CHAT_PLAN",
    "MAX_IMAGE_OUTPUT_BYTES",
    "MAX_INLINE_OUTPUT_CHARS",
    "MAX_INLINE_OUTPUT_TOKENS",
    "MAX_INLINE_QUERY_CHARS",
    "MAX_THINK_OUTPUT_CHARS",
    "MAX_THINK_OUTPUT_TOKENS",
    "MAX_THINK_PROBLEM_CHARS",
    "MAX_TTS_OUTPUT_BYTES",
    "MAX_TTS_OUTPUT_SECONDS",
    "MAX_TTS_TEXT_CHARS",
    "V1_IMAGE_OUTPUT_COUNT",
    "ImageEditRequest",
    "ImageAwaitingFunding",
    "ImageDelivered",
    "ImageDeliveryUncertain",
    "ImageExecutionPolicy",
    "ImageFeatureService",
    "ImageGenerateRequest",
    "ImageInProgress",
    "ImageInvocation",
    "ImageNotCharged",
    "ImageNotChargedReason",
    "ImageOperationCoordinator",
    "ImageOperationOutcome",
    "ImageOutput",
    "ImageProviderExecutor",
    "ImageProviderRouter",
    "ImageRefunded",
    "ImageRequest",
    "ImageSourceLoader",
    "InlineChatCompleted",
    "InlineChatExhausted",
    "InlineChatFailed",
    "InlineChatFailureReason",
    "InlineChatFeatureService",
    "InlineChatInvalid",
    "InlineChatInvocation",
    "InlineChatOutcome",
    "InlineChatPolicy",
    "InlineProviderExecution",
    "MediaContent",
    "PreparedImageEditRequest",
    "PreparedThinkRequest",
    "TextOutput",
    "ThinkExecutionPolicy",
    "ThinkFeatureService",
    "ThinkProviderExecutor",
    "ThinkRequest",
    "TtsExecutionPolicy",
    "TtsFeatureService",
    "TtsProviderExecutor",
    "TtsProviderOutput",
    "TtsRequest",
]


def __getattr__(name: str) -> Any:
    """Load application coordination only when explicitly requested."""
    if name not in _IMAGE_OPERATION_EXPORTS:
        raise AttributeError(name)
    from derp.features import image_operation

    return getattr(image_operation, name)
