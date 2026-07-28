"""Natural-language image tools pause, quote, and resume exactly once."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from pydantic_ai import DeferredToolRequests, ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models.test import TestModel

from derp.approvals import (
    HISTORY_SCHEMA_VERSION,
    DeferredToolApprovalService,
    deserialize_history,
    durable_message_history,
    serialize_deferred_request,
)
from derp.approvals.image_tools import (
    DeferredImageCall,
    DeferredImageToolError,
    ImageToolApprovalCoordinator,
    ImageToolRunContext,
    finishing_quote_from_history,
)
from derp.catalog import GoogleModelKey, get_google_model, get_openrouter_model
from derp.delivery import DeliveryTarget
from derp.execution import Feature, plan_execution
from derp.features import (
    ImageDelivered,
    ImageGenerateRequest,
    ImageOperationCoordinator,
)
from derp.llm import AgentDeps, create_chat_agent
from derp.operations import ImageGenerateQuoteInput, OperationId, QuoteEngine, QuoteId
from derp.tools.policy import (
    ActorRole,
    ChatToolAccess,
    ChatToolPolicy,
    derive_chat_tool_access,
)
from derp.tools.toolsets import create_chat_toolset, create_resumed_image_toolset


def _access() -> ChatToolAccess:
    return derive_chat_tool_access(
        ActorRole.PRIVATE_OWNER,
        ChatToolPolicy(
            expensive_tools_enabled=True,
            shared_credit_spending_enabled=True,
            shared_facts_member_edit=False,
        ),
    )


def _context() -> ImageToolRunContext:
    return ImageToolRunContext(
        requester_id=uuid4(),
        requester_telegram_id=22,
        chat_id=uuid4(),
        chat_telegram_id=-100_123,
        message_id=77,
        thread_id=9,
        business_connection_id="business-1",
    )


def _deps(
    *,
    context: ImageToolRunContext | None = None,
    coordinator: ImageOperationCoordinator | None = None,
) -> AgentDeps:
    message = MagicMock()
    message.chat.id = context.chat_telegram_id if context else -100_123
    message.from_user.id = context.requester_telegram_id if context else 22
    message.message_id = context.message_id if context else 77
    return AgentDeps(
        message=None if context else message,
        db=MagicMock(),
        bot=MagicMock(),
        tool_access=_access(),
        image_operation_coordinator=coordinator,
        image_tool_context=context,
    )


def test_image_tools_are_always_registered_as_approval_gated() -> None:
    toolset = create_chat_toolset(_access())

    assert toolset.tools["generate_image"].requires_approval is True
    assert toolset.tools["edit_image"].requires_approval is True


@pytest.mark.asyncio
async def test_initial_agent_run_returns_validated_request_without_execution() -> None:
    coordinator = MagicMock(spec=ImageOperationCoordinator)
    coordinator.run = AsyncMock()
    agent = create_chat_agent(get_google_model(GoogleModelKey.CHAT_ECONOMY))
    model = TestModel(
        call_tools=["generate_image"],
        model_name=get_google_model(GoogleModelKey.CHAT_ECONOMY).provider_model_id,
    )

    with agent.override(model=model):
        result = await agent.run(
            "Draw a lighthouse",
            deps=_deps(coordinator=coordinator),
            toolsets=[create_chat_toolset(_access())],
        )

    assert isinstance(result.output, DeferredToolRequests)
    assert len(result.output.approvals) == 1
    assert result.output.approvals[0].tool_name == "generate_image"
    assert result.output.approvals[0].args_as_dict(raise_if_invalid=True) == {
        "prompt": "a"
    }
    coordinator.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_quote_is_persisted_before_the_approval_request() -> None:
    context = _context()
    call = ToolCallPart("generate_image", {"prompt": "a lighthouse"}, "call-1")
    operation_id = OperationId.for_tool(
        feature=Feature.IMAGE_GENERATE,
        chat_id=context.chat_telegram_id,
        message_id=context.message_id,
        tool_call_id=call.tool_call_id,
    )
    quote = QuoteEngine().quote(
        quote_id=QuoteId.new(),
        operation_id=operation_id,
        plan=plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE),
        quote_input=ImageGenerateQuoteInput(
            3,
            ImageGenerateRequest("a lighthouse").resolution,
        ),
        created_at=datetime(2026, 7, 21, 12, tzinfo=UTC),
    )
    events: list[str] = []
    image_operations = MagicMock(spec=ImageOperationCoordinator)
    approvals = MagicMock(spec=DeferredToolApprovalService)

    async def ensure_quote(*args, **kwargs):
        events.append("quote")
        return quote

    async def create_request(**kwargs):
        events.append("approval")
        return MagicMock()

    image_operations.ensure_quote = AsyncMock(side_effect=ensure_quote)
    image_operations.run = AsyncMock()
    approvals.create_request = AsyncMock(side_effect=create_request)
    history = (
        ModelRequest(parts=[UserPromptPart("Draw it")]),
        ModelResponse(
            parts=[call],
            model_name=get_google_model(GoogleModelKey.CHAT_ECONOMY).provider_model_id,
        ),
    )

    prepared = await ImageToolApprovalCoordinator(
        image_operations,
        approvals,
    ).prepare(context=context, tool_call=call, original_history=history)

    assert prepared.quote is quote
    assert events == ["quote", "approval"]
    image_operations.run.assert_not_awaited()
    invocation, plan, request = image_operations.ensure_quote.await_args.args
    assert invocation.operation_id == operation_id
    assert invocation.target == DeliveryTarget(
        context.chat_telegram_id,
        context.thread_id,
        context.message_id,
        context.business_connection_id,
    )
    assert plan.feature is Feature.IMAGE_GENERATE
    assert request == ImageGenerateRequest("a lighthouse")
    assert approvals.create_request.await_args.kwargs["quote_id"] == quote.id
    assert (
        image_operations.ensure_quote.await_args.kwargs["finishing_plan"].model.key
        is GoogleModelKey.CHAT_ECONOMY
    )
    assert (
        image_operations.ensure_quote.await_args.kwargs[
            "finishing_quote_input"
        ].model_key
        is GoogleModelKey.CHAT_ECONOMY
    )


@pytest.mark.asyncio
async def test_authenticated_resume_executes_the_persisted_call_once() -> None:
    context = _context()
    first_agent = create_chat_agent(get_google_model(GoogleModelKey.CHAT_ECONOMY))
    first_model = TestModel(
        call_tools=["generate_image"],
        model_name=get_google_model(GoogleModelKey.CHAT_ECONOMY).provider_model_id,
    )
    with first_agent.override(model=first_model):
        first = await first_agent.run(
            "Draw a lighthouse",
            deps=_deps(),
            toolsets=[create_chat_toolset(_access())],
        )
    assert isinstance(first.output, DeferredToolRequests)
    tool_call = first.output.approvals[0]
    _, finishing_quote_input = finishing_quote_from_history(first.all_messages())
    context = replace(context, finishing_quote_input=finishing_quote_input)
    operation_id = OperationId.for_tool(
        feature=Feature.IMAGE_GENERATE,
        chat_id=context.chat_telegram_id,
        message_id=context.message_id,
        tool_call_id=tool_call.tool_call_id,
    )
    coordinator = MagicMock(spec=ImageOperationCoordinator)
    coordinator.run = AsyncMock(return_value=ImageDelivered(operation_id, (901,)))
    resumed_agent = create_chat_agent(get_google_model(GoogleModelKey.CHAT_ECONOMY))
    resumed_model = TestModel(
        call_tools=["generate_image"],
        model_name=get_google_model(GoogleModelKey.CHAT_ECONOMY).provider_model_id,
    )

    with resumed_agent.override(model=resumed_model):
        result = await resumed_agent.run(
            message_history=first.all_messages(),
            deferred_tool_results=first.output.build_results(approve_all=True),
            deps=_deps(context=context, coordinator=coordinator),
            toolsets=[create_resumed_image_toolset("generate_image")],
        )

    assert isinstance(result.output, str)
    coordinator.run.assert_awaited_once()
    invocation, plan, request = coordinator.run.await_args.args
    assert invocation.operation_id == operation_id
    assert invocation.requester_id == context.requester_id
    assert invocation.chat_id == context.chat_id
    assert plan.feature is Feature.IMAGE_GENERATE
    assert isinstance(request, ImageGenerateRequest)
    run_kwargs = coordinator.run.await_args.kwargs
    assert run_kwargs["allow_personal_once"] is False
    assert run_kwargs["finishing_plan"].model.key is GoogleModelKey.CHAT_ECONOMY
    assert run_kwargs["finishing_quote_input"] == finishing_quote_input


def test_deferred_call_rejects_unknown_or_extra_arguments() -> None:
    with pytest.raises(DeferredImageToolError, match="shape"):
        DeferredImageCall.parse(
            ToolCallPart(
                "generate_image",
                {"prompt": "draw", "untrusted": "override"},
                "call-1",
            ),
            source=None,
        )


def test_finishing_quote_is_identical_after_durable_history_round_trip() -> None:
    call = ToolCallPart("generate_image", {"prompt": "draw"}, "call-1")
    history = durable_message_history(
        (
            ModelRequest(parts=[UserPromptPart("Draw it")]),
            ModelResponse(
                parts=[call],
                model_name=get_google_model(
                    GoogleModelKey.CHAT_STANDARD
                ).provider_model_id,
            ),
        )
    )
    _, serialized = serialize_deferred_request(call, history)
    restored = deserialize_history(
        serialized,
        schema_version=HISTORY_SCHEMA_VERSION,
    )

    assert finishing_quote_from_history(restored) == finishing_quote_from_history(
        history
    )


def test_finishing_quote_supports_paid_multimodal_chat_plan() -> None:
    model = get_openrouter_model(GoogleModelKey.CHAT_MULTIMODAL)
    plan, quote_input = finishing_quote_from_history(
        (ModelResponse(parts=[], model_name=model.provider_model_id),)
    )

    assert plan.model is model
    assert quote_input.model_key is GoogleModelKey.CHAT_MULTIMODAL
