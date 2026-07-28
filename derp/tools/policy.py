"""Pure role and chat-policy decisions for chat tool exposure."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ActorRole(StrEnum):
    """Maximum tool entitlement established from Telegram membership."""

    PRIVATE_OWNER = "private_owner"
    MEMBER = "member"
    ADMIN = "admin"


class ChatTool(StrEnum):
    """Canonical chat tools governed by actor role and structured policy."""

    WEB_SEARCH = "web_search"
    GENERATE_IMAGE = "generate_image"
    EDIT_IMAGE = "edit_image"
    VIDEO_GENERATE = "video_generate"
    THINK_DEEP = "think_deep"
    PROPOSE_SHARED_FACT = "propose_shared_fact"
    REVIEW_SHARED_FACT = "review_shared_fact"
    DELETE_SHARED_FACT = "delete_shared_fact"


class ChatToolPolicySource(Protocol):
    """Structural view of the Chat settings used for tool authorization."""

    expensive_tools_enabled: bool
    shared_credit_spending_enabled: bool
    shared_facts_member_edit: bool


@dataclass(frozen=True, slots=True)
class ChatToolPolicy:
    """Structured chat settings relevant to tools and later settlement."""

    expensive_tools_enabled: bool
    shared_credit_spending_enabled: bool
    shared_facts_member_edit: bool

    def __post_init__(self) -> None:
        for field_name in (
            "expensive_tools_enabled",
            "shared_credit_spending_enabled",
            "shared_facts_member_edit",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise TypeError(f"{field_name} must be a bool")

    @classmethod
    def from_chat(cls, chat: ChatToolPolicySource) -> ChatToolPolicy:
        """Copy the allowlisted policy fields from a Chat-like object."""
        return cls(
            expensive_tools_enabled=chat.expensive_tools_enabled,
            shared_credit_spending_enabled=chat.shared_credit_spending_enabled,
            shared_facts_member_edit=chat.shared_facts_member_edit,
        )


@dataclass(frozen=True, slots=True)
class ChatToolAccess:
    """Deterministic tool exposure and settlement metadata for one run."""

    actor_role: ActorRole
    allowed_tools: frozenset[ChatTool]
    shared_credit_spending_enabled: bool

    def allows(self, tool: ChatTool) -> bool:
        """Return whether the model may see a canonical tool."""
        return tool in self.allowed_tools


_BASELINE_TOOLS = frozenset(
    {
        ChatTool.PROPOSE_SHARED_FACT,
    }
)
_EXPENSIVE_TOOLS = frozenset(
    {
        ChatTool.GENERATE_IMAGE,
        ChatTool.EDIT_IMAGE,
    }
)
_PRIVILEGED_FACT_TOOLS = frozenset(
    {
        ChatTool.REVIEW_SHARED_FACT,
        ChatTool.DELETE_SHARED_FACT,
    }
)


def derive_chat_tool_access(
    actor_role: ActorRole,
    policy: ChatToolPolicy,
) -> ChatToolAccess:
    """Intersect the actor's entitlement with structured chat policy.

    Wallet balances and credit sufficiency deliberately do not participate in
    exposure. The shared-credit flag is carried forward for settlement to use.
    """
    if not isinstance(actor_role, ActorRole):
        raise TypeError("actor_role must be an ActorRole")

    allowed_tools = set(_BASELINE_TOOLS)
    if policy.expensive_tools_enabled:
        allowed_tools.update(_EXPENSIVE_TOOLS)

    if actor_role in {ActorRole.PRIVATE_OWNER, ActorRole.ADMIN}:
        allowed_tools.update(_PRIVILEGED_FACT_TOOLS)
    elif policy.shared_facts_member_edit:
        allowed_tools.add(ChatTool.REVIEW_SHARED_FACT)

    return ChatToolAccess(
        actor_role=actor_role,
        allowed_tools=frozenset(allowed_tools),
        shared_credit_spending_enabled=policy.shared_credit_spending_enabled,
    )


__all__ = [
    "ActorRole",
    "ChatTool",
    "ChatToolAccess",
    "ChatToolPolicy",
    "ChatToolPolicySource",
    "derive_chat_tool_access",
]
