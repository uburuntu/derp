"""Tests for deterministic role-and-policy-derived chat tools."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic_ai import RunContext, Tool

from derp.llm.deps import AgentDeps
from derp.tools.policy import (
    ActorRole,
    ChatTool,
    ChatToolPolicy,
    derive_chat_tool_access,
)
from derp.tools.toolsets import create_chat_toolset


@dataclass(frozen=True, slots=True)
class PolicySource:
    expensive_tools_enabled: bool
    shared_credit_spending_enabled: bool
    shared_facts_member_edit: bool


class FactTools:
    def __init__(self) -> None:
        self._tools = {
            ChatTool.PROPOSE_SHARED_FACT: Tool[AgentDeps](
                propose_shared_fact,
            ),
            ChatTool.REVIEW_SHARED_FACT: Tool[AgentDeps](
                review_shared_fact,
            ),
            ChatTool.DELETE_SHARED_FACT: Tool[AgentDeps](
                delete_shared_fact,
            ),
        }

    def get_tool(self, capability: ChatTool) -> Tool[AgentDeps] | None:
        return self._tools.get(capability)


async def propose_shared_fact(
    ctx: RunContext[AgentDeps],
    fact: str,
) -> str:
    """Propose an untrusted fact for later review."""
    return fact


async def review_shared_fact(
    ctx: RunContext[AgentDeps],
    fact_id: str,
    approve: bool,
) -> str:
    """Approve or reject a proposed shared fact."""
    return f"{fact_id}:{approve}"


async def delete_shared_fact(
    ctx: RunContext[AgentDeps],
    fact_id: str,
) -> str:
    """Delete a shared fact."""
    return fact_id


def policy(
    *,
    expensive: bool = False,
    shared_credit: bool = False,
    member_edit: bool = False,
) -> ChatToolPolicy:
    return ChatToolPolicy.from_chat(
        PolicySource(
            expensive_tools_enabled=expensive,
            shared_credit_spending_enabled=shared_credit,
            shared_facts_member_edit=member_edit,
        )
    )


def test_member_gets_baseline_and_proposal_only_by_default() -> None:
    access = derive_chat_tool_access(ActorRole.MEMBER, policy())

    assert access.allowed_tools == {
        ChatTool.WEB_SEARCH,
        ChatTool.PROPOSE_SHARED_FACT,
    }
    assert set(create_chat_toolset(access, shared_fact_tools=FactTools()).tools) == {
        "propose_shared_fact",
        "web_search",
    }


def test_member_edit_enables_review_but_never_destructive_deletion() -> None:
    access = derive_chat_tool_access(
        ActorRole.MEMBER,
        policy(member_edit=True),
    )

    assert ChatTool.REVIEW_SHARED_FACT in access.allowed_tools
    assert ChatTool.DELETE_SHARED_FACT not in access.allowed_tools
    assert set(create_chat_toolset(access, shared_fact_tools=FactTools()).tools) == {
        "propose_shared_fact",
        "review_shared_fact",
        "web_search",
    }


@pytest.mark.parametrize("actor_role", [ActorRole.PRIVATE_OWNER, ActorRole.ADMIN])
def test_owner_and_admin_get_review_and_deletion(actor_role: ActorRole) -> None:
    access = derive_chat_tool_access(actor_role, policy())

    assert set(create_chat_toolset(access, shared_fact_tools=FactTools()).tools) == {
        "delete_shared_fact",
        "propose_shared_fact",
        "review_shared_fact",
        "web_search",
    }


def test_expensive_policy_adds_every_generation_and_reasoning_tool() -> None:
    access = derive_chat_tool_access(
        ActorRole.MEMBER,
        policy(expensive=True),
    )

    assert set(create_chat_toolset(access).tools) == {
        "edit_image",
        "generate_image",
        "think_deep",
        "video_generate",
        "voice_tts",
        "web_search",
    }


def test_expensive_policy_removes_generation_reasoning_and_legacy_memory() -> None:
    access = derive_chat_tool_access(ActorRole.ADMIN, policy(expensive=False))

    assert set(create_chat_toolset(access).tools) == {"web_search"}
    assert "update_chat_memory" not in create_chat_toolset(access).tools


def test_shared_credit_setting_is_settlement_metadata_not_exposure() -> None:
    disabled = derive_chat_tool_access(
        ActorRole.MEMBER,
        policy(expensive=True, shared_credit=False),
    )
    enabled = derive_chat_tool_access(
        ActorRole.MEMBER,
        policy(expensive=True, shared_credit=True),
    )

    assert disabled.allowed_tools == enabled.allowed_tools
    assert disabled.shared_credit_spending_enabled is False
    assert enabled.shared_credit_spending_enabled is True


def test_fact_provider_must_return_the_canonical_tool_name() -> None:
    class InvalidFactTools:
        def get_tool(self, capability: ChatTool) -> Tool[AgentDeps] | None:
            return Tool(propose_shared_fact, name="unexpected_name")

    access = derive_chat_tool_access(ActorRole.MEMBER, policy())

    with pytest.raises(ValueError, match="unexpected_name"):
        create_chat_toolset(access, shared_fact_tools=InvalidFactTools())


def test_policy_rejects_untyped_boolean_values() -> None:
    with pytest.raises(TypeError, match="expensive_tools_enabled"):
        ChatToolPolicy(
            expensive_tools_enabled=1,  # type: ignore[arg-type]
            shared_credit_spending_enabled=True,
            shared_facts_member_edit=False,
        )
