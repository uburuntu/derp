"""Policy-derived Pydantic AI toolsets for chat runs."""

from __future__ import annotations

from typing import Protocol

import logfire
from pydantic_ai import FunctionToolset, Tool

from derp.approvals.image_tools import EDIT_IMAGE_TOOL, GENERATE_IMAGE_TOOL
from derp.llm.deps import AgentDeps
from derp.tools.gemini_image import edit_image, generate_image
from derp.tools.policy import ChatTool, ChatToolAccess


class SharedFactToolProvider(Protocol):
    """Bridge to shared-fact tools once their adapters are implemented."""

    def get_tool(self, capability: ChatTool) -> Tool[AgentDeps] | None:
        """Return the exact canonical tool for a shared-fact capability."""
        ...


_SHARED_FACT_TOOLS = (
    ChatTool.PROPOSE_SHARED_FACT,
    ChatTool.REVIEW_SHARED_FACT,
    ChatTool.DELETE_SHARED_FACT,
)


def create_chat_toolset(
    access: ChatToolAccess,
    *,
    shared_fact_tools: SharedFactToolProvider | None = None,
) -> FunctionToolset[AgentDeps]:
    """Create exactly the tools authorized for one actor and chat policy.

    Image calls pause after framework argument validation and execute only from
    a server-authorized deferred resume. Suspended premium capabilities are not
    registered even if a caller manually includes them in ``access``.
    ``access.shared_credit_spending_enabled`` is settlement metadata and never
    changes which tools the model can see.
    """
    toolset: FunctionToolset[AgentDeps] = FunctionToolset()

    # Search stays hidden until a single disclosed provider, deadline, and
    # response-byte budget are enforced by a governed adapter.

    if access.allows(ChatTool.GENERATE_IMAGE):
        toolset.tool(generate_image, requires_approval=True)
    if access.allows(ChatTool.EDIT_IMAGE):
        toolset.tool(edit_image, requires_approval=True)

    if shared_fact_tools is not None:
        for capability in _SHARED_FACT_TOOLS:
            if not access.allows(capability):
                continue
            tool = shared_fact_tools.get_tool(capability)
            if tool is None:
                continue
            if tool.name != capability.value:
                raise ValueError(
                    f"shared-fact provider returned {tool.name!r} "
                    f"for {capability.value!r}"
                )
            toolset.add_tool(tool)

    logfire.debug(
        "chat_toolset_created",
        actor_role=access.actor_role.value,
        shared_credit_spending_enabled=access.shared_credit_spending_enabled,
        tools=sorted(toolset.tools),
    )
    return toolset


def create_resumed_image_toolset(tool_name: str) -> FunctionToolset[AgentDeps]:
    """Expose only the persisted image tool during an authenticated resume."""
    toolset: FunctionToolset[AgentDeps] = FunctionToolset()
    if tool_name == GENERATE_IMAGE_TOOL:
        toolset.tool(generate_image, requires_approval=True)
    elif tool_name == EDIT_IMAGE_TOOL:
        toolset.tool(edit_image, requires_approval=True)
    else:
        raise ValueError("unsupported deferred image tool")
    return toolset


__all__ = [
    "SharedFactToolProvider",
    "create_chat_toolset",
    "create_resumed_image_toolset",
]
