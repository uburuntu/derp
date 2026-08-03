"""Tests for chat handler."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic_ai import BinaryContent, DeferredToolRequests, ModelRequest, ToolCallPart
from pydantic_ai.exceptions import UnexpectedModelBehavior

from derp.catalog import GoogleModelKey, ModelRole
from derp.execution import Feature, plan_execution
from derp.features import ImageOperationCoordinator
from derp.features.chat_accounting import (
    ChatExecutionAlreadyHandled,
    ChatExecutionInProgress,
    ChatTurnAccounting,
    ChatTurnInvocation,
    EconomyChatExecutionGrant,
    PaidChatExecutionGrant,
)
from derp.handlers.chat import (
    ChatAgentHandler,
    _current_user_turn,
    _estimate_chat_input_tokens,
    show_context,
)
from derp.history.core import LogicalTurn, TokenEstimator
from derp.history.facts import ApprovedFact, render_approved_facts
from derp.history.media import HydratedMedia
from derp.history.service import HISTORY_WINDOWS, LoadedHistory
from derp.inference import (
    FREE_INFERENCE_PRIVACY_VERSION,
    FREE_INFERENCE_TOS_VERSION,
    InferencePrivacyMode,
)
from derp.llm import AgentContentDelivered, AgentContentUnavailable, AgentResult
from derp.llm.prompts import BASE_SYSTEM_PROMPT
from derp.operations import (
    ChatQuoteInput,
    FundingAuthorization,
    OperationState,
    QuoteEngine,
    QuoteId,
    ReservationRejection,
)


def empty_history() -> LoadedHistory:
    return LoadedHistory(messages=(), turns=(), estimated_tokens=0, source_messages=0)


async def test_operator_context_redirects_group_use_without_loading_history(
    make_message,
) -> None:
    message = make_message(text="/context", chat_type="supergroup")

    with patch("derp.handlers.chat.get_db_manager") as get_db:
        await show_context(message, None)

    get_db.assert_not_called()
    message.reply.assert_awaited_once_with(
        "Operator diagnostics are private. Open /operator in your private chat."
    )


class TestExtractMediaForAgent:
    """Tests for extract_media_for_agent function."""

    @pytest.mark.asyncio
    async def test_extracts_photo(self, make_message):
        """Test photo extraction."""
        from derp.handlers.chat import extract_media_for_agent

        message = make_message(text="")

        mock_photo = MagicMock()
        mock_photo.download = AsyncMock(return_value=b"\x89PNG...")
        mock_photo.media_type = "image/png"

        with (
            patch(
                "derp.handlers.chat.Extractor.photo", new_callable=AsyncMock
            ) as mock_photo_fn,
            patch(
                "derp.handlers.chat.Extractor.video", new_callable=AsyncMock
            ) as mock_video,
            patch(
                "derp.handlers.chat.Extractor.audio", new_callable=AsyncMock
            ) as mock_audio,
            patch(
                "derp.handlers.chat.Extractor.document", new_callable=AsyncMock
            ) as mock_doc,
        ):
            mock_photo_fn.return_value = mock_photo
            mock_video.return_value = None
            mock_audio.return_value = None
            mock_doc.return_value = None

            result = await extract_media_for_agent(message)

            assert len(result) == 1
            assert isinstance(result[0], BinaryContent)
            assert result[0].media_type == "image/png"

    @pytest.mark.asyncio
    async def test_extracts_video(self, make_message):
        """Test video extraction."""
        from derp.handlers.chat import extract_media_for_agent

        message = make_message(text="")

        mock_video_obj = MagicMock()
        mock_video_obj.download = AsyncMock(return_value=b"video_data")
        mock_video_obj.media_type = "video/mp4"

        with (
            patch(
                "derp.handlers.chat.Extractor.photo", new_callable=AsyncMock
            ) as mock_photo,
            patch(
                "derp.handlers.chat.Extractor.video", new_callable=AsyncMock
            ) as mock_video,
            patch(
                "derp.handlers.chat.Extractor.audio", new_callable=AsyncMock
            ) as mock_audio,
            patch(
                "derp.handlers.chat.Extractor.document", new_callable=AsyncMock
            ) as mock_doc,
        ):
            mock_photo.return_value = None
            mock_video.return_value = mock_video_obj
            mock_audio.return_value = None
            mock_doc.return_value = None

            result = await extract_media_for_agent(message)

            assert len(result) == 1
            assert result[0].media_type == "video/mp4"

    @pytest.mark.asyncio
    async def test_extracts_audio(self, make_message):
        """Test audio extraction."""
        from derp.handlers.chat import extract_media_for_agent

        message = make_message(text="")

        mock_audio_obj = MagicMock()
        mock_audio_obj.download = AsyncMock(return_value=b"audio_data")
        mock_audio_obj.media_type = "audio/ogg"

        with (
            patch(
                "derp.handlers.chat.Extractor.photo", new_callable=AsyncMock
            ) as mock_photo,
            patch(
                "derp.handlers.chat.Extractor.video", new_callable=AsyncMock
            ) as mock_video,
            patch(
                "derp.handlers.chat.Extractor.audio", new_callable=AsyncMock
            ) as mock_audio,
            patch(
                "derp.handlers.chat.Extractor.document", new_callable=AsyncMock
            ) as mock_doc,
        ):
            mock_photo.return_value = None
            mock_video.return_value = None
            mock_audio.return_value = mock_audio_obj
            mock_doc.return_value = None

            result = await extract_media_for_agent(message)

            assert len(result) == 1
            assert result[0].media_type == "audio/ogg"

    @pytest.mark.asyncio
    async def test_extracts_pdf_document(self, make_message):
        """Test PDF document extraction."""
        from derp.handlers.chat import extract_media_for_agent

        message = make_message(text="")

        mock_doc_obj = MagicMock()
        mock_doc_obj.download = AsyncMock(return_value=b"%PDF...")
        mock_doc_obj.media_type = "application/pdf"

        with (
            patch(
                "derp.handlers.chat.Extractor.photo", new_callable=AsyncMock
            ) as mock_photo,
            patch(
                "derp.handlers.chat.Extractor.video", new_callable=AsyncMock
            ) as mock_video,
            patch(
                "derp.handlers.chat.Extractor.audio", new_callable=AsyncMock
            ) as mock_audio,
            patch(
                "derp.handlers.chat.Extractor.document", new_callable=AsyncMock
            ) as mock_doc,
        ):
            mock_photo.return_value = None
            mock_video.return_value = None
            mock_audio.return_value = None
            mock_doc.return_value = mock_doc_obj

            result = await extract_media_for_agent(message)

            assert len(result) == 1
            assert result[0].media_type == "application/pdf"

    @pytest.mark.asyncio
    async def test_skips_non_pdf_document(self, make_message):
        """Test non-PDF documents are skipped."""
        from derp.handlers.chat import extract_media_for_agent

        message = make_message(text="")

        mock_doc_obj = MagicMock()
        mock_doc_obj.media_type = "application/zip"

        with (
            patch(
                "derp.handlers.chat.Extractor.photo", new_callable=AsyncMock
            ) as mock_photo,
            patch(
                "derp.handlers.chat.Extractor.video", new_callable=AsyncMock
            ) as mock_video,
            patch(
                "derp.handlers.chat.Extractor.audio", new_callable=AsyncMock
            ) as mock_audio,
            patch(
                "derp.handlers.chat.Extractor.document", new_callable=AsyncMock
            ) as mock_doc,
        ):
            mock_photo.return_value = None
            mock_video.return_value = None
            mock_audio.return_value = None
            mock_doc.return_value = mock_doc_obj

            result = await extract_media_for_agent(message)

            assert len(result) == 0

    @pytest.mark.asyncio
    async def test_handles_download_failure(self, make_message):
        """Test graceful handling of download failures."""
        from derp.handlers.chat import extract_media_for_agent

        message = make_message(text="")

        mock_photo = MagicMock()
        mock_photo.download = AsyncMock(side_effect=Exception("Network error"))

        with (
            patch(
                "derp.handlers.chat.Extractor.photo", new_callable=AsyncMock
            ) as mock_photo_fn,
            patch(
                "derp.handlers.chat.Extractor.video", new_callable=AsyncMock
            ) as mock_video,
            patch(
                "derp.handlers.chat.Extractor.audio", new_callable=AsyncMock
            ) as mock_audio,
            patch(
                "derp.handlers.chat.Extractor.document", new_callable=AsyncMock
            ) as mock_doc,
        ):
            mock_photo_fn.return_value = mock_photo
            mock_video.return_value = None
            mock_audio.return_value = None
            mock_doc.return_value = None

            result = await extract_media_for_agent(message)

            # Should not raise, just skip
            assert len(result) == 0


class TestBuildContextPrompt:
    """Tests for build_context_prompt function."""

    @pytest.mark.asyncio
    async def test_includes_chat_info(self, make_message, mock_db_client):
        """Test context includes chat information."""
        from derp.handlers.chat import build_context_prompt

        message = make_message(text="Hello")
        message.chat.id = -100123
        message.chat.type = "supergroup"
        message.chat.title = "Test Chat"

        with patch(
            "derp.handlers.chat._load_history",
            new=AsyncMock(return_value=empty_history()),
        ):
            result = await build_context_prompt(
                message, mock_db_client, context_limit=10
            )

            assert "Test Chat" in result or "supergroup" in result

    @pytest.mark.asyncio
    async def test_includes_recent_messages(self, make_message, mock_db_client):
        """Test context includes recent chat history."""
        from derp.handlers.chat import build_context_prompt

        message = make_message(text="current")
        history = LoadedHistory(
            messages=(ModelRequest.user_text_prompt("prior native message"),),
            turns=(),
            estimated_tokens=10,
            source_messages=1,
        )
        with patch(
            "derp.handlers.chat._load_history",
            new=AsyncMock(return_value=history),
        ):
            result = await build_context_prompt(message, mock_db_client)

        assert "prior native message" in result

    @pytest.mark.asyncio
    async def test_includes_current_message(self, make_message, mock_db_client):
        """Test context includes current message text."""
        from derp.handlers.chat import build_context_prompt

        message = make_message(text="What is Python?")
        message.chat.id = -100123
        message.from_user.username = "asker"

        with patch(
            "derp.handlers.chat._load_history",
            new=AsyncMock(return_value=empty_history()),
        ):
            result = await build_context_prompt(
                message, mock_db_client, context_limit=10
            )

            assert "What is Python?" in result

    @pytest.mark.asyncio
    async def test_respects_context_limit(self, make_message, mock_db_client):
        """Test context respects limit parameter."""
        from derp.handlers.chat import build_context_prompt

        message = make_message(text="Hello")
        message.chat.id = -100123

        with patch(
            "derp.handlers.chat._load_history",
            new=AsyncMock(return_value=empty_history()),
        ) as mock_load:
            await build_context_prompt(message, mock_db_client, context_limit=5)

            mock_load.assert_awaited_once()
            window = mock_load.await_args.args[2]
            assert window.max_turns == 5


def _paid_decision(invocation: ChatTurnInvocation) -> PaidChatExecutionGrant:
    plan = invocation.paid_plan
    quote = QuoteEngine().quote(
        quote_id=QuoteId.new(),
        operation_id=invocation.operation_id,
        plan=plan,
        quote_input=ChatQuoteInput(invocation.estimated_input_tokens),
        created_at=_HANDLER_NOW,
    )
    return PaidChatExecutionGrant(
        invocation.operation_id,
        quote,
        plan,
        FundingAuthorization.PRIVATE,
    )


def _economy_decision(invocation: ChatTurnInvocation) -> EconomyChatExecutionGrant:
    paid = _paid_decision(invocation)
    return EconomyChatExecutionGrant(
        invocation.operation_id,
        paid.quote,
        invocation.fallback_plan
        or plan_execution(Feature.CHAT, GoogleModelKey.CHAT_ECONOMY),
        ReservationRejection.PERSONAL_CONSENT_REQUIRED,
    )


@dataclass(slots=True)
class ChatHandlerEnvironment:
    message: Any
    db: Any
    user_model: Any
    chat_model: Any
    accounting: MagicMock
    agent: MagicMock
    run_result: MagicMock
    agent_result: MagicMock
    load_history: AsyncMock
    probe: LoadedHistory
    selected: LoadedHistory
    approved_facts: tuple[ApprovedFact, ...]
    create_agent: MagicMock
    present_approvals: AsyncMock
    image_operations: MagicMock
    report_exception: MagicMock
    media_gateway: MagicMock
    inference_recorder: MagicMock

    async def run(self) -> Any:
        return await ChatAgentHandler(
            self.message,
            db=self.db,
            bot=self.message.bot,
            user_model=self.user_model,
            chat_model=self.chat_model,
            chat_turn_accounting=self.accounting,
            media_gateway=self.media_gateway,
            image_operation_coordinator=self.image_operations,
            deferred_tool_approval_service=MagicMock(),
            inference_recorder=self.inference_recorder,
        ).handle()


_HANDLER_NOW = datetime(
    2026,
    7,
    21,
    12,
    tzinfo=UTC,
)


@pytest.fixture
def chat_handler_environment(
    make_message,
    mock_db_client,
    mock_user_model,
    mock_chat_model,
    monkeypatch,
) -> ChatHandlerEnvironment:
    message = make_message(
        message_id=73,
        text="How should we structure this?",
        user_id=901,
        chat_id=902,
        chat_type="private",
        message_thread_id=17,
    )
    message.date = _HANDLER_NOW
    message.business_connection_id = None
    user_model = mock_user_model(user_id=uuid4(), telegram_id=901)
    chat_model = mock_chat_model(
        chat_id=uuid4(),
        telegram_id=902,
        chat_type="private",
        admin_policy="Prefer exact technical answers.",
    )
    accounting = MagicMock(spec=ChatTurnAccounting)
    accounting.authorize = AsyncMock()
    accounting.capture_success = AsyncMock()
    accounting.release_delivery_failure = AsyncMock()
    accounting.release_provider_failure = AsyncMock()

    probe = LoadedHistory(
        messages=(ModelRequest.user_text_prompt("standard probe"),),
        turns=(),
        estimated_tokens=137,
        source_messages=1,
    )
    selected = LoadedHistory(
        messages=(ModelRequest.user_text_prompt("selected history"),),
        turns=(),
        estimated_tokens=41,
        source_messages=1,
    )
    load_history = AsyncMock(side_effect=[probe, selected])
    approved_facts = (ApprovedFact(uuid4(), "The deployment region is London."),)

    run_result = MagicMock()
    run_result.output = "Delivered answer"
    run_result.new_messages.return_value = ()
    run_result.all_messages.return_value = ()
    agent = MagicMock()
    agent.run = AsyncMock(return_value=run_result)
    agent.parallel_tool_call_execution_mode.return_value = MagicMock()
    create_agent = MagicMock(return_value=agent)

    agent_result = MagicMock(spec=AgentResult)
    agent_result.text = "Delivered answer"
    agent_result.images = []
    agent_result.has_content = True
    agent_result.reply_to = AsyncMock(return_value=AgentContentDelivered(message))

    toolset = MagicMock()
    toolset.tools = {}
    present_approvals = AsyncMock(return_value=message)
    image_operations = MagicMock(spec=ImageOperationCoordinator)
    report_exception = MagicMock()
    media_gateway = MagicMock()
    inference_recorder = MagicMock()
    inference_recorder.start = AsyncMock(return_value=MagicMock())
    inference_recorder.succeed = AsyncMock(return_value=())
    inference_recorder.fail = AsyncMock()

    monkeypatch.setattr(
        "derp.handlers.chat.ensure_group_context_notice",
        AsyncMock(),
    )
    monkeypatch.setattr("derp.handlers.chat._load_history", load_history)
    monkeypatch.setattr(
        "derp.handlers.chat._load_approved_facts",
        AsyncMock(return_value=approved_facts),
    )
    monkeypatch.setattr(
        "derp.handlers.chat.live_image_source",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "derp.handlers.chat.extract_media_for_agent",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "derp.handlers.chat._hydrate_current_media",
        AsyncMock(return_value=HydratedMedia({}, 0)),
    )
    monkeypatch.setattr("derp.handlers.chat.create_chat_agent", create_agent)
    monkeypatch.setattr(
        "derp.handlers.chat.create_chat_toolset",
        MagicMock(return_value=toolset),
    )
    monkeypatch.setattr(
        "derp.handlers.chat.AgentResult.from_run_result",
        MagicMock(return_value=agent_result),
    )
    monkeypatch.setattr(
        "derp.handlers.chat.present_image_approvals",
        present_approvals,
    )
    monkeypatch.setattr("derp.handlers.chat.report_exception", report_exception)

    return ChatHandlerEnvironment(
        message=message,
        db=mock_db_client,
        user_model=user_model,
        chat_model=chat_model,
        accounting=accounting,
        agent=agent,
        run_result=run_result,
        agent_result=agent_result,
        load_history=load_history,
        probe=probe,
        selected=selected,
        approved_facts=approved_facts,
        create_agent=create_agent,
        present_approvals=present_approvals,
        image_operations=image_operations,
        report_exception=report_exception,
        media_gateway=media_gateway,
        inference_recorder=inference_recorder,
    )


async def test_paid_chat_captures_only_after_result_delivery(
    chat_handler_environment: ChatHandlerEnvironment,
) -> None:
    env = chat_handler_environment
    env.accounting.authorize.side_effect = _paid_decision

    delivered = await env.run()

    invocation = env.accounting.authorize.await_args.args[0]
    assert delivered is env.message
    env.agent_result.reply_to.assert_awaited_once_with(env.message)
    env.accounting.capture_success.assert_awaited_once_with(invocation.operation_id)
    env.accounting.release_delivery_failure.assert_not_awaited()
    env.accounting.release_provider_failure.assert_not_awaited()
    assert env.create_agent.call_args.args[0].model.key is GoogleModelKey.CHAT_STANDARD


async def test_provider_failure_releases_paid_turn_before_fallback(
    chat_handler_environment: ChatHandlerEnvironment,
) -> None:
    env = chat_handler_environment
    env.accounting.authorize.side_effect = _paid_decision
    env.agent.run.side_effect = UnexpectedModelBehavior("provider rejected output")

    await env.run()

    invocation = env.accounting.authorize.await_args.args[0]
    env.accounting.release_provider_failure.assert_awaited_once_with(
        invocation.operation_id
    )
    env.accounting.release_delivery_failure.assert_not_awaited()
    env.accounting.capture_success.assert_not_awaited()
    assert env.message.reply.await_count == 2
    assert env.message.reply.await_args.args[0] == (
        "I couldn't answer that. You weren't charged. Try again."
    )


async def test_consented_free_turn_runs_without_daily_admission(
    chat_handler_environment: ChatHandlerEnvironment,
) -> None:
    env = chat_handler_environment
    env.user_model.inference_privacy_mode = (
        InferencePrivacyMode.ALLOW_NON_ZDR_FREE.value
    )
    env.user_model.inference_privacy_revision = 2
    env.user_model.free_inference_tos_version = FREE_INFERENCE_TOS_VERSION
    env.user_model.free_inference_privacy_version = FREE_INFERENCE_PRIVACY_VERSION
    env.user_model.free_inference_accepted_at = _HANDLER_NOW
    env.user_model.free_inference_revoked_at = None
    env.accounting.authorize.side_effect = _economy_decision
    await env.run()

    invocation = env.accounting.authorize.await_args.args[0]
    assert invocation.fallback_plan is not None
    assert invocation.fallback_plan.model.key is ModelRole.FREE_TEXT
    env.agent.run.assert_awaited_once()


async def test_selected_history_failure_releases_before_provider_call(
    chat_handler_environment: ChatHandlerEnvironment,
) -> None:
    env = chat_handler_environment
    env.accounting.authorize.side_effect = _paid_decision
    env.load_history.side_effect = [env.probe, RuntimeError("history unavailable")]

    await env.run()

    invocation = env.accounting.authorize.await_args.args[0]
    env.accounting.release_provider_failure.assert_awaited_once_with(
        invocation.operation_id
    )
    env.accounting.release_delivery_failure.assert_not_awaited()
    env.agent.run.assert_not_awaited()
    env.accounting.capture_success.assert_not_awaited()


async def test_delivery_failure_releases_paid_turn_and_sends_fallback(
    chat_handler_environment: ChatHandlerEnvironment,
) -> None:
    env = chat_handler_environment
    env.accounting.authorize.side_effect = _paid_decision
    env.agent_result.reply_to.side_effect = RuntimeError("telegram unavailable")

    await env.run()

    invocation = env.accounting.authorize.await_args.args[0]
    env.accounting.release_delivery_failure.assert_awaited_once_with(
        invocation.operation_id
    )
    env.accounting.release_provider_failure.assert_not_awaited()
    env.accounting.capture_success.assert_not_awaited()
    assert env.message.reply.await_count == 2
    assert env.message.reply.await_args.args[0] == (
        "I couldn't answer that. You weren't charged. Try again."
    )


async def test_generic_delivery_notice_releases_paid_turn_without_capture(
    chat_handler_environment: ChatHandlerEnvironment,
) -> None:
    env = chat_handler_environment
    env.accounting.authorize.side_effect = _paid_decision
    env.agent_result.reply_to.return_value = AgentContentUnavailable(env.message)

    delivered = await env.run()

    invocation = env.accounting.authorize.await_args.args[0]
    assert delivered is env.message
    env.accounting.release_delivery_failure.assert_awaited_once_with(
        invocation.operation_id
    )
    env.accounting.release_provider_failure.assert_not_awaited()
    env.accounting.capture_success.assert_not_awaited()


@pytest.mark.parametrize("reaction_fails", [False, True])
async def test_unusable_response_always_releases_after_best_effort_reaction(
    chat_handler_environment: ChatHandlerEnvironment,
    reaction_fails: bool,
) -> None:
    env = chat_handler_environment
    env.accounting.authorize.side_effect = _paid_decision
    env.agent_result.has_content = False
    env.agent_result.text = None
    if reaction_fails:
        env.message.react.side_effect = RuntimeError("reaction rejected")

    assert await env.run() is None

    invocation = env.accounting.authorize.await_args.args[0]
    env.accounting.release_provider_failure.assert_awaited_once_with(
        invocation.operation_id
    )
    env.accounting.capture_success.assert_not_awaited()


async def test_consent_fallback_runs_economy_without_paid_settlement(
    chat_handler_environment: ChatHandlerEnvironment,
) -> None:
    env = chat_handler_environment
    env.accounting.authorize.side_effect = _economy_decision

    delivered = await env.run()

    assert delivered is env.message
    assert env.create_agent.call_args.args[0].model.key is GoogleModelKey.CHAT_ECONOMY
    env.accounting.capture_success.assert_not_awaited()
    env.accounting.release_provider_failure.assert_not_awaited()
    selected_window = env.load_history.await_args_list[1].args[2]
    assert selected_window is HISTORY_WINDOWS[GoogleModelKey.CHAT_ECONOMY]


async def test_deferred_image_panel_captures_independent_paid_base_turn(
    chat_handler_environment: ChatHandlerEnvironment,
) -> None:
    env = chat_handler_environment
    env.accounting.authorize.side_effect = _paid_decision
    call = ToolCallPart("generate_image", {"prompt": "a lighthouse"}, "call-1")
    env.run_result.output = DeferredToolRequests(approvals=[call])

    delivered = await env.run()

    invocation = env.accounting.authorize.await_args.args[0]
    assert delivered is env.message
    env.present_approvals.assert_awaited_once()
    env.accounting.capture_success.assert_awaited_once_with(invocation.operation_id)
    env.accounting.release_provider_failure.assert_not_awaited()
    env.image_operations.run.assert_not_awaited()


@pytest.mark.parametrize("existing", [False, True])
async def test_duplicate_paid_turn_is_suppressed_before_provider(
    chat_handler_environment: ChatHandlerEnvironment,
    existing: bool,
) -> None:
    env = chat_handler_environment

    def decision(invocation: ChatTurnInvocation):
        paid = _paid_decision(invocation)
        if existing:
            return ChatExecutionAlreadyHandled(
                invocation.operation_id,
                paid.quote,
                OperationState.CAPTURED,
                FundingAuthorization.PRIVATE,
            )
        return ChatExecutionInProgress(
            invocation.operation_id,
            paid.quote,
            FundingAuthorization.PRIVATE,
        )

    env.accounting.authorize.side_effect = decision

    assert await env.run() is None

    env.create_agent.assert_not_called()
    env.agent.run.assert_not_awaited()
    env.accounting.capture_success.assert_not_awaited()
    env.accounting.release_provider_failure.assert_not_awaited()
    assert env.load_history.await_count == 1


async def test_quote_probe_is_standard_no_media_and_tokens_are_deterministic(
    chat_handler_environment: ChatHandlerEnvironment,
) -> None:
    env = chat_handler_environment
    events: list[str] = []
    histories = iter((env.probe, env.selected))

    async def load_history(*_args: object) -> LoadedHistory:
        history = next(histories)
        events.append("probe" if history is env.probe else "selected")
        return history

    async def authorize(invocation: ChatTurnInvocation):
        events.append("authorize")
        return _economy_decision(invocation)

    async def run_provider(*_args: object, **_kwargs: object):
        events.append("provider")
        return env.run_result

    env.load_history.side_effect = load_history
    env.accounting.authorize.side_effect = authorize
    env.agent.run.side_effect = run_provider

    await env.run()

    probe_call = env.load_history.await_args_list[0]
    assert probe_call.args == (
        env.message,
        env.db,
        HISTORY_WINDOWS[GoogleModelKey.CHAT_STANDARD],
    )
    assert env.load_history.await_args_list[1].args[3] is env.media_gateway
    assert events == ["probe", "authorize", "selected", "provider"]
    invocation = env.accounting.authorize.await_args.args[0]
    estimator = TokenEstimator()
    current_turn = _current_user_turn(env.message)
    expected = (
        env.probe.estimated_tokens
        + estimator.message_overhead
        + estimator.part_overhead
        + estimator.estimate_text(
            BASE_SYSTEM_PROMPT
            + "\n\n## Admin Chat Policy\n"
            + env.chat_model.admin_policy
        )
        + estimator.estimate_turn(LogicalTurn(request=current_turn))
        + estimator.part_overhead
        + estimator.estimate_text(render_approved_facts(env.approved_facts))
        + 2_048
    )
    assert invocation.estimated_input_tokens == expected
    assert invocation.thread_id == 17
    assert invocation.estimated_input_tokens == _estimate_chat_input_tokens(
        history=env.probe,
        current_turn=current_turn,
        approved_facts=env.approved_facts,
        chat_model=env.chat_model,
    )


async def test_capture_failure_after_visible_delivery_does_not_retry_or_release(
    chat_handler_environment: ChatHandlerEnvironment,
) -> None:
    env = chat_handler_environment
    env.accounting.authorize.side_effect = _paid_decision
    env.accounting.capture_success.side_effect = RuntimeError("database unavailable")

    delivered = await env.run()

    assert delivered is env.message
    env.agent_result.reply_to.assert_awaited_once_with(env.message)
    env.accounting.release_provider_failure.assert_not_awaited()
    env.message.reply.assert_awaited_once_with(
        "Private models use credits. Reply to any answer with /info for the details."
    )
    assert env.report_exception.call_args.args[0] == (
        "chat_turn_capture_failed_after_delivery"
    )
