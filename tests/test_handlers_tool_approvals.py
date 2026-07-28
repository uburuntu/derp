"""Telegram deferred-image controls preserve actor, chat, and topic scope."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import uuid4

import pytest
from aiogram.types import CallbackQuery, PhotoSize
from pydantic_ai import DeferredToolRequests, ModelRequest, ModelResponse, ToolCallPart
from pydantic_ai.messages import UserPromptPart

from derp.approvals import (
    ApprovalAuthorizationError,
    DecisionDisposition,
    DeferredToolApprovalService,
)
from derp.approvals.image_tools import (
    ImageToolRunContext,
    load_persisted_image_source,
)
from derp.billing import CommercePolicy
from derp.billing.products import DEFAULT_PRODUCT_CATALOG
from derp.billing.telegram import PurchaseCallback, PurchaseTargetCode
from derp.catalog import GoogleModelKey, ImageResolution, InferenceProvider
from derp.db import DatabaseManager
from derp.delivery import DeliveryResendCallback
from derp.execution import Feature, plan_execution
from derp.features import ImageAwaitingFunding, ImageDelivered, ImageDeliveryUncertain
from derp.handlers.tool_approvals import (
    ImageApprovalAction,
    ImageApprovalCallback,
    ImageResumeResult,
    _chat_purchase_callback,
    _render_image_outcome,
    _resume_approved_image,
    approve_image_tool,
    deny_image_tool,
    present_image_approvals,
)
from derp.history.persistence import project_persisted_message
from derp.history.snapshot import (
    CaptureKind,
    MessageDirection,
    SnapshotRole,
    project_message_snapshot,
)
from derp.operations import (
    ImageGenerateQuoteInput,
    OperationId,
    OperationLedger,
    QuoteEngine,
    QuoteId,
    ReservationRejection,
)

CALLBACK_CAPABILITY = "a" * 43
INVALID_CAPABILITY = "b" * 43


def _callback(message, user) -> CallbackQuery:
    callback = MagicMock(spec=CallbackQuery)
    callback.message = message
    callback.from_user = user
    callback.bot = MagicMock()
    callback.answer = AsyncMock()
    return callback


@pytest.mark.asyncio
async def test_cancel_uses_exact_callback_actor_chat_and_topic(
    make_message,
    make_user,
) -> None:
    message = make_message(
        text="approval",
        chat_id=-100_123,
        message_thread_id=9,
    )
    message.edit_text = AsyncMock()
    callback = _callback(message, make_user(id=22))
    service = MagicMock(spec=DeferredToolApprovalService)
    service.deny = AsyncMock(
        return_value=SimpleNamespace(disposition=DecisionDisposition.APPLIED)
    )

    await deny_image_tool(
        callback,
        ImageApprovalCallback(
            action=ImageApprovalAction.CANCEL,
            token=CALLBACK_CAPABILITY,
        ),
        MagicMock(spec=DatabaseManager),
        service,
    )

    capability = service.deny.await_args.args[0]
    assert capability.requester_telegram_id == 22
    assert capability.chat_telegram_id == -100_123
    assert capability.thread_id == 9
    assert capability.token == CALLBACK_CAPABILITY
    callback.answer.assert_awaited_once_with()
    message.edit_text.assert_awaited_once_with(
        "Canceled. You weren't charged.",
        reply_markup=None,
    )


@pytest.mark.asyncio
async def test_invalid_capability_never_changes_the_control_message(
    make_message,
    make_user,
) -> None:
    message = make_message(text="approval")
    message.edit_text = AsyncMock()
    callback = _callback(message, make_user(id=22))
    service = MagicMock(spec=DeferredToolApprovalService)
    service.deny = AsyncMock(side_effect=ApprovalAuthorizationError("invalid"))

    await deny_image_tool(
        callback,
        ImageApprovalCallback(
            action=ImageApprovalAction.CANCEL,
            token=INVALID_CAPABILITY,
        ),
        MagicMock(spec=DatabaseManager),
        service,
    )

    callback.answer.assert_awaited_once_with(
        "I can't use this request in this chat.",
        show_alert=True,
    )
    message.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_denial_replaces_controls_with_not_charged_state(
    make_message,
    make_user,
) -> None:
    message = make_message(text="approval")
    message.edit_text = AsyncMock()
    callback = _callback(message, make_user(id=22))
    service = MagicMock(spec=DeferredToolApprovalService)
    service.deny = AsyncMock(
        return_value=SimpleNamespace(disposition=DecisionDisposition.EXPIRED)
    )

    await deny_image_tool(
        callback,
        ImageApprovalCallback(
            action=ImageApprovalAction.CANCEL,
            token=CALLBACK_CAPABILITY,
        ),
        MagicMock(spec=DatabaseManager),
        service,
    )

    message.edit_text.assert_awaited_once_with(
        "This image request expired. You weren't charged. Send it again.",
        reply_markup=None,
    )


@pytest.mark.asyncio
async def test_uncertain_delivery_keeps_only_authenticated_resend_control(
    make_message,
) -> None:
    message = make_message(text="approval", chat_id=-100_123)
    message.edit_text = AsyncMock()
    operation_id = OperationId.for_command(
        feature=Feature.IMAGE_GENERATE,
        chat_id=-100_123,
        message_id=1,
    )

    await _render_image_outcome(
        message,
        ImageDeliveryUncertain(
            operation_id=operation_id,
            code="telegram_timeout",
            resend_token="r" * 43,
        ),
    )

    edit = message.edit_text.await_args
    assert "may already be in the chat" in edit.args[0]
    assert "You won't be charged again" in edit.args[0]
    buttons = edit.kwargs["reply_markup"].inline_keyboard
    assert len(buttons) == 1
    assert len(buttons[0]) == 1
    callback = DeliveryResendCallback.unpack(buttons[0][0].callback_data)
    assert callback.token == "r" * 43
    assert len(buttons[0][0].callback_data.encode()) <= 64


@pytest.mark.asyncio
async def test_run_claim_is_completed_only_after_successful_resume(
    make_message,
    make_user,
) -> None:
    message = make_message(text="approval", chat_id=-100_123)
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()
    callback = _callback(message, make_user(id=22))
    lease = object()
    service = MagicMock(spec=DeferredToolApprovalService)
    service.approve = AsyncMock(
        return_value=SimpleNamespace(disposition=DecisionDisposition.APPLIED)
    )
    service.claim_resume = AsyncMock(return_value=lease)
    service.mark_resumed = AsyncMock()
    service.release_resume = AsyncMock()

    with patch(
        "derp.handlers.tool_approvals._resume_approved_image",
        new=AsyncMock(
            return_value=ImageResumeResult(
                "",
                ImageDelivered(
                    OperationId.for_command(
                        feature=Feature.IMAGE_GENERATE, chat_id=-100_123, message_id=1
                    ),
                    (99,),
                ),
            )
        ),
    ) as resume:
        await approve_image_tool(
            callback,
            ImageApprovalCallback(
                action=ImageApprovalAction.RUN,
                token=CALLBACK_CAPABILITY,
            ),
            MagicMock(spec=DatabaseManager),
            MagicMock(),
            deferred_tool_approval_service=service,
        )

    resume.assert_awaited_once()
    service.mark_resumed.assert_awaited_once_with(lease)
    service.release_resume.assert_not_awaited()
    assert message.edit_text.await_args_list == [
        call("Creating your image...", reply_markup=None),
    ]
    message.delete.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_failed_resume_releases_lease_and_offers_run_only(
    make_message,
    make_user,
) -> None:
    message = make_message(text="approval")
    message.edit_text = AsyncMock()
    callback = _callback(message, make_user(id=22))
    snapshot = SimpleNamespace(request_id=uuid4(), operation_id=uuid4())
    lease = SimpleNamespace(snapshot=snapshot)
    service = MagicMock(spec=DeferredToolApprovalService)
    service.approve = AsyncMock(
        return_value=SimpleNamespace(disposition=DecisionDisposition.APPLIED)
    )
    service.claim_resume = AsyncMock(return_value=lease)
    service.mark_resumed = AsyncMock()
    service.release_resume = AsyncMock(return_value=True)

    with patch(
        "derp.handlers.tool_approvals._resume_approved_image",
        new=AsyncMock(side_effect=RuntimeError("provider unavailable")),
    ):
        await approve_image_tool(
            callback,
            ImageApprovalCallback(
                action=ImageApprovalAction.RUN,
                token=CALLBACK_CAPABILITY,
            ),
            MagicMock(spec=DatabaseManager),
            MagicMock(),
            deferred_tool_approval_service=service,
        )

    service.release_resume.assert_awaited_once_with(lease)
    service.mark_resumed.assert_not_awaited()
    retry_markup = message.edit_text.await_args_list[-1].kwargs["reply_markup"]
    assert len(retry_markup.inline_keyboard[0]) == 1
    packed = ImageApprovalCallback.unpack(
        retry_markup.inline_keyboard[0][0].callback_data
    )
    assert packed.action is ImageApprovalAction.RUN
    assert packed.token == CALLBACK_CAPABILITY


@pytest.mark.asyncio
async def test_shared_funding_failure_keeps_approval_runnable_and_explicit(
    make_message,
    make_user,
) -> None:
    message = make_message(text="approval", chat_id=-100_123)
    message.edit_text = AsyncMock()
    callback = _callback(message, make_user(id=22))
    lease = object()
    service = MagicMock(spec=DeferredToolApprovalService)
    service.approve = AsyncMock(
        return_value=SimpleNamespace(disposition=DecisionDisposition.APPLIED)
    )
    service.claim_resume = AsyncMock(return_value=lease)
    service.mark_resumed = AsyncMock()
    service.release_resume = AsyncMock(return_value=True)
    operation_id = OperationId.for_command(
        feature=Feature.IMAGE_GENERATE,
        chat_id=-100_123,
        message_id=1,
    )
    quote = QuoteEngine().quote(
        quote_id=QuoteId.new(),
        operation_id=operation_id,
        plan=plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE),
        quote_input=ImageGenerateQuoteInput(1, ImageResolution.ONE_K),
        created_at=datetime(2026, 7, 21, 12, tzinfo=UTC),
    )
    outcome = ImageAwaitingFunding(
        operation_id,
        quote,
        ReservationRejection.PERSONAL_CONSENT_REQUIRED,
    )
    chat_model = SimpleNamespace(type="supergroup")

    with patch(
        "derp.handlers.tool_approvals._resume_approved_image",
        new=AsyncMock(return_value=ImageResumeResult("", outcome)),
    ) as resume:
        await approve_image_tool(
            callback,
            ImageApprovalCallback(
                action=ImageApprovalAction.RUN,
                token=CALLBACK_CAPABILITY,
            ),
            MagicMock(spec=DatabaseManager),
            MagicMock(),
            chat_model=chat_model,
            deferred_tool_approval_service=service,
            commerce_policy=CommercePolicy(public_intake_enabled=True),
        )

    assert resume.await_args.kwargs["allow_personal_once"] is False
    service.release_resume.assert_awaited_once_with(lease)
    service.mark_resumed.assert_not_awaited()
    funding_edit = message.edit_text.await_args_list[-1]
    assert "You weren't charged" in funding_edit.args[0]
    actions = {
        ImageApprovalCallback.unpack(button.callback_data).action
        for row in funding_edit.kwargs["reply_markup"].inline_keyboard
        for button in row
        if button.callback_data.startswith("img-tool:")
    }
    assert actions == {
        ImageApprovalAction.ALWAYS_HERE,
        ImageApprovalAction.RUN,
        ImageApprovalAction.USE_PERSONAL_ONCE,
    }
    purchase_buttons = [
        PurchaseCallback.unpack(button.callback_data)
        for row in funding_edit.kwargs["reply_markup"].inline_keyboard
        for button in row
        if button.callback_data.startswith("buy:")
    ]
    assert len(purchase_buttons) == 1
    assert purchase_buttons[0].target is PurchaseTargetCode.CHAT
    product = DEFAULT_PRODUCT_CATALOG.current_top_ups[purchase_buttons[0].product_id]
    assert product.credits >= quote.credits


def test_chat_purchase_callback_uses_smallest_covering_pack() -> None:
    callback = _chat_purchase_callback(51)

    assert callback is not None
    purchase = PurchaseCallback.unpack(callback)
    assert purchase.target is PurchaseTargetCode.CHAT
    assert purchase.product_id == "basic"


def test_chat_purchase_callback_omits_action_beyond_largest_pack() -> None:
    largest_pack = max(
        DEFAULT_PRODUCT_CATALOG.current_top_ups.values(),
        key=lambda product: product.credits,
    )

    assert _chat_purchase_callback(largest_pack.credits + 1) is None


@pytest.mark.asyncio
async def test_closed_commerce_does_not_advertise_chat_purchase(
    make_message,
    make_user,
) -> None:
    message = make_message(text="approval", chat_id=-100_123)
    message.edit_text = AsyncMock()
    callback = _callback(message, make_user(id=22))
    lease = object()
    service = MagicMock(spec=DeferredToolApprovalService)
    service.approve = AsyncMock(
        return_value=SimpleNamespace(disposition=DecisionDisposition.APPLIED)
    )
    service.claim_resume = AsyncMock(return_value=lease)
    service.mark_resumed = AsyncMock()
    service.release_resume = AsyncMock(return_value=True)
    operation_id = OperationId.for_command(
        feature=Feature.IMAGE_GENERATE,
        chat_id=-100_123,
        message_id=1,
    )
    quote = QuoteEngine().quote(
        quote_id=QuoteId.new(),
        operation_id=operation_id,
        plan=plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE),
        quote_input=ImageGenerateQuoteInput(1, ImageResolution.ONE_K),
        created_at=datetime(2026, 7, 21, 12, tzinfo=UTC),
    )
    outcome = ImageAwaitingFunding(
        operation_id,
        quote,
        ReservationRejection.PERSONAL_CONSENT_REQUIRED,
    )

    with patch(
        "derp.handlers.tool_approvals._resume_approved_image",
        new=AsyncMock(return_value=ImageResumeResult("", outcome)),
    ):
        await approve_image_tool(
            callback,
            ImageApprovalCallback(
                action=ImageApprovalAction.RUN,
                token=CALLBACK_CAPABILITY,
            ),
            MagicMock(spec=DatabaseManager),
            MagicMock(),
            chat_model=SimpleNamespace(type="supergroup"),
            deferred_tool_approval_service=service,
            commerce_policy=CommercePolicy(public_intake_enabled=False),
        )

    rows = message.edit_text.await_args_list[-1].kwargs["reply_markup"].inline_keyboard
    buttons = [button for row in rows for button in row]
    assert all(not button.callback_data.startswith("buy:") for button in buttons)


@pytest.mark.asyncio
async def test_always_here_grants_scoped_consent_before_resume(
    make_message,
    make_user,
) -> None:
    message = make_message(text="approval", chat_id=-100_123)
    message.edit_text = AsyncMock()
    message.delete = AsyncMock()
    callback = _callback(message, make_user(id=22))
    requester_id = uuid4()
    chat_id = uuid4()
    snapshot = SimpleNamespace(
        requester_id=requester_id,
        requester_telegram_id=22,
        chat_id=chat_id,
        chat_telegram_id=-100_123,
    )
    lease = object()
    service = MagicMock(spec=DeferredToolApprovalService)
    service.approve = AsyncMock(
        return_value=SimpleNamespace(
            disposition=DecisionDisposition.APPLIED,
            snapshot=snapshot,
        )
    )
    service.claim_resume = AsyncMock(return_value=lease)
    service.mark_resumed = AsyncMock()
    service.release_resume = AsyncMock()
    ledger = MagicMock(spec=OperationLedger)
    ledger.grant_personal_consent = AsyncMock()
    user_model = SimpleNamespace(id=requester_id, telegram_id=22)
    chat_model = SimpleNamespace(id=chat_id, telegram_id=-100_123)

    with patch(
        "derp.handlers.tool_approvals._resume_approved_image",
        new=AsyncMock(
            return_value=ImageResumeResult(
                "",
                ImageDelivered(
                    OperationId.for_command(
                        feature=Feature.IMAGE_GENERATE,
                        chat_id=-100_123,
                        message_id=1,
                    ),
                    (99,),
                ),
            )
        ),
    ) as resume:
        await approve_image_tool(
            callback,
            ImageApprovalCallback(
                action=ImageApprovalAction.ALWAYS_HERE,
                token=CALLBACK_CAPABILITY,
            ),
            MagicMock(spec=DatabaseManager),
            MagicMock(),
            user_model=user_model,
            chat_model=chat_model,
            deferred_tool_approval_service=service,
            operation_ledger=ledger,
        )

    ledger.grant_personal_consent.assert_awaited_once_with(requester_id, chat_id)
    assert resume.await_args.kwargs["allow_personal_once"] is False
    service.mark_resumed.assert_awaited_once_with(lease)


@pytest.mark.asyncio
async def test_funding_preflight_does_not_spend_a_finishing_model_call(
    make_message,
    make_user,
) -> None:
    message = make_message(
        text="approval",
        chat_id=-100_123,
        chat_type="supergroup",
        business_connection_id=None,
    )
    callback = _callback(message, make_user(id=22))
    requester_id = uuid4()
    chat_id = uuid4()
    tool_call = ToolCallPart("generate_image", {"prompt": "draw"}, "call-1")
    history = (
        ModelRequest(parts=[UserPromptPart("Draw it")]),
        ModelResponse(
            parts=[tool_call],
            model_name="gemini-3.1-flash-lite",
        ),
    )
    operation_id = OperationId.for_tool(
        feature=Feature.IMAGE_GENERATE,
        chat_id=-100_123,
        message_id=77,
        tool_call_id="call-1",
    )
    quote = QuoteEngine().quote(
        quote_id=QuoteId.new(),
        operation_id=operation_id,
        plan=plan_execution(Feature.IMAGE_GENERATE, GoogleModelKey.IMAGE),
        quote_input=ImageGenerateQuoteInput(1, ImageResolution.ONE_K),
        created_at=datetime(2026, 7, 21, 12, tzinfo=UTC),
    )
    funding = ImageAwaitingFunding(
        operation_id,
        quote,
        ReservationRejection.PERSONAL_CONSENT_REQUIRED,
    )
    lease = SimpleNamespace(
        snapshot=SimpleNamespace(
            operation_id=operation_id,
            requester_id=requester_id,
            requester_telegram_id=22,
            chat_id=chat_id,
            chat_telegram_id=-100_123,
            message_id=77,
            thread_id=None,
            tool_name="generate_image",
            tool_call_id="call-1",
        ),
        build_run_input=lambda: SimpleNamespace(
            message_history=history,
            deferred_tool_results=object(),
        ),
    )
    image_operations = MagicMock()
    image_operations.execution_plan_for_quote = AsyncMock(
        return_value=plan_execution(
            Feature.IMAGE_GENERATE,
            GoogleModelKey.IMAGE,
            provider=InferenceProvider.GOOGLE,
        )
    )
    image_operations.run = AsyncMock(return_value=funding)
    user_model = SimpleNamespace(id=requester_id)
    chat_model = SimpleNamespace(
        id=chat_id,
        expensive_tools_enabled=True,
        shared_credit_spending_enabled=True,
        shared_facts_member_edit=False,
    )

    with (
        patch(
            "derp.handlers.tool_approvals.load_persisted_image_source",
            new=AsyncMock(return_value=None),
        ),
        patch("derp.handlers.tool_approvals.create_chat_agent") as create_agent,
    ):
        result = await _resume_approved_image(
            callback=callback,
            message=message,
            lease=lease,
            db=MagicMock(spec=DatabaseManager),
            image_operations=image_operations,
            user_model=user_model,
            chat_model=chat_model,
            actor_role_resolver=None,
            allow_personal_once=False,
        )

    assert result == ImageResumeResult("", funding)
    image_operations.execution_plan_for_quote.assert_awaited_once_with(
        operation_id,
        Feature.IMAGE_GENERATE,
    )
    assert image_operations.run.await_args.kwargs["allow_personal_once"] is False
    create_agent.assert_not_called()


@pytest.mark.asyncio
async def test_presenter_shows_exact_quote_without_prompt_content(
    make_message,
) -> None:
    message = make_message(text="private prompt sentinel")
    call_part = ToolCallPart(
        "generate_image",
        {"prompt": "private prompt sentinel"},
        "call-1",
    )
    requests = DeferredToolRequests(approvals=[call_part])
    prepared = SimpleNamespace(
        call=SimpleNamespace(feature=Feature.IMAGE_GENERATE),
        quote=SimpleNamespace(credits=7),
        handle=SimpleNamespace(callback_token="a" * 43),
    )

    with patch(
        "derp.handlers.tool_approvals.ImageToolApprovalCoordinator.prepare",
        new=AsyncMock(return_value=prepared),
    ):
        await present_image_approvals(
            message=message,
            requests=requests,
            original_history=(
                ModelRequest(parts=[UserPromptPart("private prompt sentinel")]),
                ModelResponse(parts=[call_part]),
            ),
            context=MagicMock(spec=ImageToolRunContext),
            image_operations=MagicMock(),
            approvals=MagicMock(),
        )

    text = message.reply.await_args.args[0]
    markup = message.reply.await_args.kwargs["reply_markup"]
    assert text == "Create this image for 7 credits?"
    assert "private prompt sentinel" not in text
    assert markup.inline_keyboard[0][0].text == "Create image"
    assert markup.inline_keyboard[0][1].text == "Cancel"
    assert len(markup.inline_keyboard[0][0].callback_data) <= 64
    assert len(markup.inline_keyboard[0][1].callback_data) <= 64


@pytest.mark.asyncio
async def test_presenter_rejects_sibling_approvals_without_creating_records(
    make_message,
) -> None:
    message = make_message(text="two images")
    first = ToolCallPart("generate_image", {"prompt": "first"}, "call-1")
    second = ToolCallPart("generate_image", {"prompt": "second"}, "call-2")

    with patch(
        "derp.handlers.tool_approvals.ImageToolApprovalCoordinator.prepare",
        new=AsyncMock(),
    ) as prepare:
        await present_image_approvals(
            message=message,
            requests=DeferredToolRequests(approvals=[first, second]),
            original_history=(
                ModelRequest(parts=[UserPromptPart("two images")]),
                ModelResponse(parts=[first, second]),
            ),
            context=MagicMock(spec=ImageToolRunContext),
            image_operations=MagicMock(),
            approvals=MagicMock(),
        )

    prepare.assert_not_awaited()
    text = message.reply.await_args.args[0]
    assert "one paid image at a time" in text
    assert "You weren't charged" in text


@pytest.mark.asyncio
async def test_restart_recovers_replied_photo_reference_without_downloading(
    make_message,
) -> None:
    photo = PhotoSize(
        file_id="telegram-file",
        file_unique_id="stable-file",
        width=1024,
        height=768,
        file_size=321,
    )
    replied = make_message(content_type="photo", photo=[photo], message_id=10)
    request = make_message(text="edit it", reply_to_message=replied, message_id=11)
    current_row = SimpleNamespace(
        source_snapshot=project_persisted_message(
            project_message_snapshot(
                request,
                role=SnapshotRole.USER,
                direction=MessageDirection.INBOUND,
                capture=CaptureKind.EXPLICIT,
            )
        ).source_snapshot,
        reply_to_message_id=10,
    )
    replied_row = SimpleNamespace(
        source_snapshot=project_persisted_message(
            project_message_snapshot(
                replied,
                role=SnapshotRole.USER,
                direction=MessageDirection.INBOUND,
                capture=CaptureKind.EXPLICIT,
            )
        ).source_snapshot,
        reply_to_message_id=None,
    )
    session = MagicMock()
    session.scalar = AsyncMock(side_effect=[current_row, replied_row])

    @asynccontextmanager
    async def read_session():
        yield session

    db = MagicMock(spec=DatabaseManager)
    db.read_session = read_session

    source = await load_persisted_image_source(
        db,
        chat_id=uuid4(),
        message_id=11,
    )

    assert source is not None
    assert source.file_id == "telegram-file"
    assert source.file_unique_id == "stable-file"
    assert source.metadata.mime_type == "image/jpeg"
    assert source.metadata.file_size == 321
    assert session.scalar.await_count == 2
