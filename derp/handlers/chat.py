"""AI-powered chat response handler using Pydantic-AI.

This handler processes chat messages and generates AI responses using
the provider-agnostic Pydantic-AI infrastructure with tools like
DuckDuckGo search and chat memory.

The handler is credit-aware:
- Free access: Uses the economy chat role with 10-message context
- Paid access: Uses the standard chat role with 100-message context
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import logfire
from aiogram import Bot, F, Router, flags
from aiogram.filters import Command
from aiogram.handlers import MessageHandler
from aiogram.types import Message, ReactionTypeEmoji
from aiogram.utils.i18n import gettext as _
from pydantic_ai import BinaryContent, DeferredToolRequests, UsageLimits
from pydantic_ai.exceptions import (
    ModelHTTPError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)

from derp.approvals import DeferredToolApprovalService
from derp.approvals.image_tools import ImageToolRunContext
from derp.catalog import GoogleModelKey
from derp.common.extractor import Extractor
from derp.config import settings
from derp.db import (
    DatabaseManager,
    get_db_manager,
    list_approved_shared_facts,
    store_tool_transcript,
)
from derp.features import ImageOperationCoordinator
from derp.features.chat_accounting import (
    ChatExecutionAlreadyHandled,
    ChatExecutionInProgress,
    ChatTurnAccounting,
    ChatTurnInvocation,
    EconomyChatExecutionGrant,
    PaidChatExecutionGrant,
)
from derp.filters import DerpMentionFilter
from derp.handlers.context_settings import ensure_group_context_notice
from derp.handlers.tool_approvals import (
    approval_service,
    live_image_source,
    present_image_approvals,
)
from derp.handlers.tool_approvals import (
    router as tool_approvals_router,
)
from derp.history.capture import capture_outbound_history
from derp.history.core import (
    AttachmentReference,
    LogicalTurn,
    Speaker,
    TokenEstimator,
    UserTextTurn,
    render_user_content,
)
from derp.history.facts import ApprovedFact, render_approved_facts
from derp.history.media import (
    DEFAULT_AGGREGATE_MEDIA_BYTES,
    HydratedMedia,
    hydrate_media,
    hydration_candidate,
)
from derp.history.persistence import project_persisted_message
from derp.history.service import (
    HISTORY_WINDOWS,
    ConversationHistoryService,
    HistoryWindow,
    LoadedHistory,
)
from derp.history.snapshot import (
    CaptureKind,
    MessageDirection,
    SnapshotRole,
    TelegramMessageSnapshot,
    project_message_snapshot,
)
from derp.history.transcript import extract_tool_rounds, serialize_tool_rounds
from derp.llm import (
    RELAXED_SAFETY_SETTINGS,
    AgentContentDelivered,
    AgentContentUnavailable,
    AgentDeps,
    AgentResult,
    create_chat_agent,
)
from derp.llm.prompts import BASE_SYSTEM_PROMPT
from derp.media import MediaGateway
from derp.models import Chat as ChatModel
from derp.models import User as UserModel
from derp.observability import report_exception
from derp.operations import OperationId
from derp.tools import create_chat_toolset
from derp.tools.authorization import ActorRoleResolver
from derp.tools.policy import (
    ActorRole,
    ChatToolPolicy,
    derive_chat_tool_access,
)
from derp.tools.shared_facts import SharedFactTools

router = Router(name="chat")
router.include_router(tool_approvals_router)

# Provider serialization and registered tool schemas are outside history estimation.
_CHAT_RUNTIME_OVERHEAD_TOKENS = 2_048


@logfire.instrument("extract_media", extract_args=False)
async def extract_media_for_agent(
    message: Message,
    media_gateway: MediaGateway | None = None,
    *,
    max_total_bytes: int = DEFAULT_AGGREGATE_MEDIA_BYTES,
) -> list[BinaryContent]:
    """Extract supported media from message for agent processing.

    Converts Telegram media to Pydantic-AI BinaryContent format.
    """
    if max_total_bytes < 0:
        raise ValueError("Aggregate media byte limit must not be negative")
    if media_gateway is not None:
        hydrated = await _hydrate_current_media(
            message,
            media_gateway,
            max_total_bytes=max_total_bytes,
        )
        return list(hydrated.content.values())

    media_parts: list[BinaryContent] = []

    # Extract photo (includes image documents and static stickers)
    if photo := await Extractor.photo(message):
        try:
            image_data = await photo.download()
            media_parts.append(
                BinaryContent(
                    data=image_data,
                    media_type=photo.media_type or "image/jpeg",
                )
            )
            logfire.debug(
                "photo_extracted",
                media_type=photo.media_type,
                size=len(image_data),
            )
        except Exception:
            report_exception("photo_download_failed")

    # Extract video (includes video stickers, animations, video notes)
    if video := await Extractor.video(message):
        try:
            video_data = await video.download()
            media_parts.append(
                BinaryContent(
                    data=video_data,
                    media_type=video.media_type or "video/mp4",
                )
            )
            logfire.debug(
                "video_extracted",
                media_type=video.media_type,
                size=len(video_data),
            )
        except Exception:
            report_exception("video_download_failed")

    # Extract audio (includes audio files and voice messages)
    if audio := await Extractor.audio(message):
        try:
            audio_data = await audio.download()
            media_parts.append(
                BinaryContent(
                    data=audio_data,
                    media_type=audio.media_type or "audio/ogg",
                )
            )
            logfire.debug(
                "audio_extracted",
                media_type=audio.media_type,
                size=len(audio_data),
            )
        except Exception:
            report_exception("audio_download_failed")

    # Extract document (PDF only for now)
    if (
        document := await Extractor.document(message)
    ) and document.media_type == "application/pdf":
        try:
            document_data = await document.download()
            media_parts.append(
                BinaryContent(
                    data=document_data,
                    media_type=document.media_type,
                )
            )
            logfire.debug(
                "document_extracted",
                media_type=document.media_type,
                size=len(document_data),
            )
        except Exception:
            report_exception("document_download_failed")

    bounded_parts: list[BinaryContent] = []
    retained_bytes = 0
    for part in media_parts:
        if retained_bytes + len(part.data) > max_total_bytes:
            continue
        bounded_parts.append(part)
        retained_bytes += len(part.data)
    return bounded_parts


async def _hydrate_current_media(
    message: Message,
    media_gateway: MediaGateway,
    *,
    max_total_bytes: int,
) -> HydratedMedia:
    snapshot = _attachment_source_snapshot(message)
    candidates = [
        candidate
        for attachment in snapshot.attachments
        if (candidate := hydration_candidate(attachment)) is not None
    ]
    return await hydrate_media(
        gateway=media_gateway,
        bot=message.bot,
        candidates=candidates,
        max_items=8,
        max_total_bytes=max_total_bytes,
    )


def _attachment_source_snapshot(message: Message) -> TelegramMessageSnapshot:
    current = project_message_snapshot(
        message,
        role=SnapshotRole.USER,
        direction=MessageDirection.INBOUND,
        capture=CaptureKind.EXPLICIT,
    )
    if current.attachments or not message.reply_to_message:
        return current
    return project_message_snapshot(
        message.reply_to_message,
        role=SnapshotRole.USER,
        direction=MessageDirection.INBOUND,
        capture=CaptureKind.EXPLICIT,
    )


@logfire.instrument("build_context", extract_args=False)
async def build_context_prompt(
    message: Message,
    db: DatabaseManager,
    context_limit: int = 100,
) -> str:
    """Render the scoped native history for the admin `/context` diagnostic."""
    window = HistoryWindow(
        max_turns=context_limit,
        max_tokens=max(4_096, context_limit * 2_048),
        query_limit=max(context_limit, context_limit * 3),
    )
    history = await _load_history(message, db, window)
    current = render_user_content(_current_user_turn(message))
    title = message.chat.title or message.chat.username or str(message.chat.id)
    parts = [
        f"scope={message.chat.id}:{message.message_thread_id or 0} chat={title}",
        *(repr(native) for native in history.messages),
        current,
    ]
    return "\n".join(parts)


async def _load_history(
    message: Message,
    db: DatabaseManager,
    window: HistoryWindow,
    media_gateway: MediaGateway | None = None,
) -> LoadedHistory:
    return await ConversationHistoryService(
        db,
        media_gateway=media_gateway,
        bot=message.bot if media_gateway else None,
    ).load_before(
        chat_id=message.chat.id,
        thread_id=message.message_thread_id,
        current_date=message.date,
        current_message_id=message.message_id,
        window=window,
    )


def _current_user_turn(message: Message) -> UserTextTurn:
    snapshot = project_message_snapshot(
        message,
        role=SnapshotRole.USER,
        direction=MessageDirection.INBOUND,
        capture=CaptureKind.EXPLICIT,
    )
    attachment_snapshot = _attachment_source_snapshot(message)
    projection = project_persisted_message(snapshot)
    sender = snapshot.sender
    display_name = "Unknown sender"
    if sender:
        display_name = (
            sender.title
            or (f"@{sender.username}" if sender.username else None)
            or " ".join(part for part in (sender.first_name, sender.last_name) if part)
            or str(sender.id)
        )
    return UserTextTurn(
        source_message_id=snapshot.message_id,
        timestamp=snapshot.sent_at,
        speaker=Speaker(id=sender and sender.id, display_name=display_name),
        text=projection.text,
        attachments=tuple(
            AttachmentReference(
                media_type=attachment.media_type.value,
                file_id=attachment.file_id,
                file_unique_id=attachment.file_unique_id,
            )
            for attachment in attachment_snapshot.attachments
        ),
    )


def _current_user_prompt(
    message: Message,
    media_by_reference: Mapping[AttachmentReference, BinaryContent],
    approved_facts: tuple[ApprovedFact, ...] = (),
) -> list[str | BinaryContent]:
    turn = _current_user_turn(message)
    media_parts = [
        media_by_reference[reference]
        for reference in turn.attachments
        if reference in media_by_reference
    ]
    current = render_user_content(
        turn,
        available_attachments=media_by_reference,
    )
    text_parts = [render_approved_facts(approved_facts)] if approved_facts else []
    return [*text_parts, current, *media_parts]


async def _load_approved_facts(
    db: DatabaseManager,
    chat_model: ChatModel | None,
    thread_id: int | None,
) -> tuple[ApprovedFact, ...]:
    if chat_model is None:
        return ()
    async with db.read_session() as session:
        facts = await list_approved_shared_facts(
            session,
            chat_id=chat_model.id,
            thread_id=thread_id,
        )
    return tuple(ApprovedFact(id=fact.id, text=fact.fact_text) for fact in facts)


def _history_media_bytes(history: LoadedHistory) -> int:
    total = 0
    for message in history.messages:
        for part in message.parts:
            content = getattr(part, "content", None)
            values = content if isinstance(content, list) else [content]
            total += sum(
                len(value.data) for value in values if isinstance(value, BinaryContent)
            )
    return total


def _estimate_chat_input_tokens(
    *,
    history: LoadedHistory,
    current_turn: UserTextTurn,
    approved_facts: tuple[ApprovedFact, ...],
    chat_model: ChatModel,
) -> int:
    """Estimate the stable text envelope used to quote one ordinary turn."""
    estimator = TokenEstimator()
    system_prompt = BASE_SYSTEM_PROMPT
    if chat_model.admin_policy:
        system_prompt += "\n\n## Admin Chat Policy\n" + chat_model.admin_policy

    system_tokens = (
        estimator.message_overhead
        + estimator.part_overhead
        + estimator.estimate_text(system_prompt)
    )
    current_tokens = estimator.estimate_turn(LogicalTurn(request=current_turn))
    shared_fact_tokens = 0
    if approved_facts:
        shared_fact_tokens = estimator.part_overhead + estimator.estimate_text(
            render_approved_facts(approved_facts)
        )
    return (
        history.estimated_tokens
        + system_tokens
        + current_tokens
        + shared_fact_tokens
        + _CHAT_RUNTIME_OVERHEAD_TOKENS
    )


async def _release_paid_chat_turn(
    accounting: ChatTurnAccounting | None,
    operation_id: OperationId | None,
    message: Message,
) -> None:
    if accounting is None or operation_id is None:
        return
    try:
        await accounting.release_provider_failure(operation_id)
    except Exception as exc:
        report_exception(
            "chat_turn_release_failed",
            exception=exc,
            operation_id=str(operation_id),
            chat_id=message.chat.id,
            message_id=message.message_id,
        )


async def _capture_delivered_paid_chat_turn(
    accounting: ChatTurnAccounting | None,
    operation_id: OperationId | None,
    message: Message,
) -> None:
    if accounting is None or operation_id is None:
        return
    try:
        await accounting.capture_success(operation_id)
    except Exception as exc:
        report_exception(
            "chat_turn_capture_failed_after_delivery",
            exception=exc,
            operation_id=str(operation_id),
            chat_id=message.chat.id,
            message_id=message.message_id,
        )


async def _release_undelivered_paid_chat_turn(
    accounting: ChatTurnAccounting | None,
    operation_id: OperationId | None,
    message: Message,
) -> None:
    if accounting is None or operation_id is None:
        return
    try:
        await accounting.release_delivery_failure(operation_id)
    except Exception as exc:
        report_exception(
            "chat_turn_delivery_release_failed",
            exception=exc,
            operation_id=str(operation_id),
            chat_id=message.chat.id,
            message_id=message.message_id,
        )


@router.message(Command("context"), F.from_user.id.in_(settings.admin_ids))
async def show_context(message: Message, chat_model: ChatModel | None) -> None:
    """Admin command to show the context that would be sent to the agent."""
    db = get_db_manager()
    ctx = await build_context_prompt(message, db)
    char_count = len(ctx)
    message_count = ctx.count('"message_id"')
    chars = _("{count} character", "{count} characters", char_count).format(
        count=char_count
    )
    messages = _("{count} message", "{count} messages", message_count).format(
        count=message_count
    )
    stats = _("Context: {chars}, {messages}").format(
        chars=chars,
        messages=messages,
    )
    await message.reply(stats)


@router.message(DerpMentionFilter())
@router.message(Command("derp"))
@router.message(F.chat.type == "private")
@router.message(F.reply_to_message.from_user.id == settings.bot_id)
@flags.chat_action
class ChatAgentHandler(MessageHandler):
    """Run one atomically quoted chat turn with its selected context window."""

    async def handle(self) -> Any:
        """Handle messages using the Pydantic-AI chat agent."""
        db: DatabaseManager = self.data.get("db") or get_db_manager()
        bot: Bot = self.data.get("bot") or self.event.bot
        user_model: UserModel | None = self.data.get("user_model")
        chat_model: ChatModel | None = self.data.get("chat_model")
        chat_turn_accounting: ChatTurnAccounting | None = self.data.get(
            "chat_turn_accounting"
        )
        media_gateway: MediaGateway | None = self.data.get("media_gateway")
        role_resolver: ActorRoleResolver | None = self.data.get("actor_role_resolver")
        image_operation_coordinator: ImageOperationCoordinator | None = self.data.get(
            "image_operation_coordinator"
        )
        deferred_tool_approval_service: DeferredToolApprovalService | None = (
            self.data.get("deferred_tool_approval_service")
        )
        paid_operation_id: OperationId | None = None
        try:
            await ensure_group_context_notice(
                self.event,
                chat_model=chat_model,
                db=db,
                bot=bot,
            )
            if user_model is None or chat_model is None or chat_turn_accounting is None:
                return await self.event.reply(
                    _(
                        "I couldn't verify your account. Please try again. "
                        "You weren't charged."
                    )
                )

            if role_resolver is not None and self.event.from_user is not None:
                actor_role = await role_resolver.resolve(
                    chat_id=self.event.chat.id,
                    chat_type=self.event.chat.type,
                    user_id=self.event.from_user.id,
                )
            elif self.event.chat.type == "private":
                actor_role = ActorRole.PRIVATE_OWNER
            else:
                actor_role = ActorRole.MEMBER
            tool_policy = ChatToolPolicy.from_chat(chat_model)
            tool_access = derive_chat_tool_access(actor_role, tool_policy)

            approved_facts = await _load_approved_facts(
                db,
                chat_model,
                self.event.message_thread_id,
            )
            standard_probe = await _load_history(
                self.event,
                db,
                HISTORY_WINDOWS[GoogleModelKey.CHAT_STANDARD],
            )
            current_turn = _current_user_turn(self.event)
            estimated_input_tokens = _estimate_chat_input_tokens(
                history=standard_probe,
                current_turn=current_turn,
                approved_facts=approved_facts,
                chat_model=chat_model,
            )
            invocation = ChatTurnInvocation(
                telegram_chat_id=self.event.chat.id,
                telegram_message_id=self.event.message_id,
                requester_id=user_model.id,
                chat_id=chat_model.id,
                thread_id=self.event.message_thread_id,
                estimated_input_tokens=estimated_input_tokens,
            )
            decision = await chat_turn_accounting.authorize(invocation)
            if isinstance(
                decision,
                (ChatExecutionInProgress, ChatExecutionAlreadyHandled),
            ):
                logfire.info(
                    "chat_turn_duplicate_suppressed",
                    operation_id=str(decision.operation_id),
                    outcome=type(decision).__name__,
                )
                return None
            if isinstance(decision, PaidChatExecutionGrant):
                paid_operation_id = decision.operation_id
            elif not isinstance(decision, EconomyChatExecutionGrant):
                raise RuntimeError("chat accounting returned an unsupported decision")
            plan = decision.plan
            history_window = HISTORY_WINDOWS[plan.model.key]

            image_tool_context = ImageToolRunContext(
                requester_id=user_model.id,
                requester_telegram_id=(
                    self.event.from_user.id
                    if self.event.from_user is not None
                    else user_model.telegram_id
                ),
                chat_id=chat_model.id,
                chat_telegram_id=self.event.chat.id,
                message_id=self.event.message_id,
                thread_id=self.event.message_thread_id,
                business_connection_id=self.event.business_connection_id,
                source=await live_image_source(self.event),
            )

            deps = AgentDeps(
                message=self.event,
                db=db,
                bot=bot,
                user_model=user_model,
                chat_model=chat_model,
                model=plan.model,
                history_window=history_window,
                tool_access=tool_access,
                image_operation_coordinator=image_operation_coordinator,
                image_tool_context=image_tool_context,
            )

            with logfire.span(
                "chat_agent_run",
                _tags=["agent", "chat"],
                telegram_chat_id=self.event.chat.id,
                telegram_user_id=self.event.from_user and self.event.from_user.id,
                telegram_message_id=self.event.message_id,
                model_key=deps.model.key.value,
                model=deps.model.provider_model_id,
                history_max_turns=history_window.max_turns,
                history_max_tokens=history_window.max_tokens,
            ) as span:
                history = await _load_history(
                    self.event,
                    db,
                    history_window,
                    media_gateway,
                )
                span.set_attribute("derp.context_messages", len(history.messages))
                span.set_attribute("derp.context_turns", len(history.turns))
                span.set_attribute(
                    "derp.context_estimated_tokens", history.estimated_tokens
                )
                span.set_attribute("derp.history_media", history.hydrated_media)
                span.set_attribute(
                    "derp.history_media_failures", history.media_failures
                )
                span.set_attribute("derp.approved_shared_facts", len(approved_facts))

                history_media_bytes = _history_media_bytes(history)
                current_media_budget = max(
                    0,
                    DEFAULT_AGGREGATE_MEDIA_BYTES - history_media_bytes,
                )
                if media_gateway is not None:
                    hydrated_current = await _hydrate_current_media(
                        self.event,
                        media_gateway,
                        max_total_bytes=current_media_budget,
                    )
                    media_by_reference = hydrated_current.content
                else:
                    media_parts = await extract_media_for_agent(self.event)
                    media_by_reference = dict(
                        zip(current_turn.attachments, media_parts, strict=False)
                    )
                span.set_attribute("derp.has_media", bool(media_by_reference))
                span.set_attribute("derp.media_count", len(media_by_reference))
                span.set_attribute(
                    "derp.media_bytes",
                    history_media_bytes
                    + sum(len(item.data) for item in media_by_reference.values()),
                )

                user_prompt = _current_user_prompt(
                    self.event,
                    media_by_reference,
                    approved_facts,
                )

                agent = create_chat_agent(plan)
                toolset = create_chat_toolset(
                    tool_access,
                    shared_fact_tools=SharedFactTools(),
                )

                logfire.info(
                    "running_agent",
                    model_key=deps.model.key.value,
                    model=deps.model.provider_model_id,
                    history_max_turns=history_window.max_turns,
                    history_estimated_tokens=history.estimated_tokens,
                    actor_role=actor_role.value,
                    tools=len(toolset.tools),
                )

                with capture_outbound_history():
                    with agent.parallel_tool_call_execution_mode("sequential"):
                        result = await agent.run(
                            user_prompt,
                            message_history=history.messages,
                            deps=deps,
                            toolsets=[toolset],
                            usage_limits=UsageLimits(
                                request_limit=5,
                                tool_calls_limit=3,
                                input_tokens_limit=plan.model.input_token_limit,
                                output_tokens_limit=plan.model.output_token_limit,
                            ),
                            model_settings=RELAXED_SAFETY_SETTINGS,
                        )

                tool_rounds = extract_tool_rounds(result.new_messages())
                span.set_attribute("derp.tool_rounds", len(tool_rounds))
                if tool_rounds:
                    try:
                        async with db.session() as session:
                            await store_tool_transcript(
                                session,
                                chat_telegram_id=self.event.chat.id,
                                telegram_message_id=self.event.message_id,
                                tool_rounds=serialize_tool_rounds(tool_rounds),
                            )
                    except Exception as exc:
                        report_exception(
                            "tool_transcript_persist_failed",
                            exception=exc,
                            level="warning",
                            chat_id=self.event.chat.id,
                            message_id=self.event.message_id,
                        )

                if isinstance(result.output, DeferredToolRequests):
                    if (
                        image_operation_coordinator is None
                        or image_tool_context is None
                    ):
                        raise RuntimeError(
                            "deferred image tools require durable operation context"
                        )
                    delivered = await present_image_approvals(
                        message=self.event,
                        requests=result.output,
                        original_history=result.all_messages(),
                        context=image_tool_context,
                        image_operations=image_operation_coordinator,
                        approvals=deferred_tool_approval_service
                        or approval_service(db),
                    )
                    if delivered is None:
                        await _release_undelivered_paid_chat_turn(
                            chat_turn_accounting,
                            paid_operation_id,
                            self.event,
                        )
                        return None
                    await _capture_delivered_paid_chat_turn(
                        chat_turn_accounting,
                        paid_operation_id,
                        self.event,
                    )
                    return delivered

                agent_result = AgentResult.from_run_result(result)

                span.set_attribute("derp.response_has_text", bool(agent_result.text))
                span.set_attribute("derp.response_images", len(agent_result.images))

                if not agent_result.has_content:
                    try:
                        await self.event.react(reaction=[ReactionTypeEmoji(emoji="👌")])
                        logfire.debug("empty_response_reacted")
                    except Exception:
                        logfire.debug("empty_response_react_failed")
                    await _release_paid_chat_turn(
                        chat_turn_accounting,
                        paid_operation_id,
                        self.event,
                    )
                    return None

                try:
                    with capture_outbound_history():
                        delivery = await agent_result.reply_to(self.event)
                except Exception:
                    await _release_undelivered_paid_chat_turn(
                        chat_turn_accounting,
                        paid_operation_id,
                        self.event,
                    )
                    paid_operation_id = None
                    raise
                if delivery is None:
                    await _release_undelivered_paid_chat_turn(
                        chat_turn_accounting,
                        paid_operation_id,
                        self.event,
                    )
                    return None
                if isinstance(delivery, AgentContentUnavailable):
                    await _release_undelivered_paid_chat_turn(
                        chat_turn_accounting,
                        paid_operation_id,
                        self.event,
                    )
                    return delivery.notice
                if not isinstance(delivery, AgentContentDelivered):
                    raise TypeError("chat delivery returned an unsupported outcome")
                await _capture_delivered_paid_chat_turn(
                    chat_turn_accounting,
                    paid_operation_id,
                    self.event,
                )
                return delivery.message

        except ModelHTTPError as exc:
            await _release_paid_chat_turn(
                chat_turn_accounting,
                paid_operation_id,
                self.event,
            )
            if exc.status_code == 429:
                logfire.warning(
                    "chat_rate_limited",
                    status_code=exc.status_code,
                    model=exc.model_name,
                )
                return await self.event.reply(
                    _(
                        "I'm busy right now. Try again in about a minute. "
                        "You weren't charged."
                    )
                )
            report_exception(
                "chat_model_http_error",
                exception=exc,
                status_code=exc.status_code,
            )
            return await self.event.reply(
                _("I couldn't answer that. Please try again. You weren't charged.")
            )
        except UsageLimitExceeded as exc:
            await _release_paid_chat_turn(
                chat_turn_accounting,
                paid_operation_id,
                self.event,
            )
            report_exception(
                "agent_usage_limit_exceeded",
                exception=exc,
                level="warning",
            )
            return await self.event.reply(
                _(
                    "That request became too complex. Try a simpler version. "
                    "You weren't charged."
                )
            )
        except UnexpectedModelBehavior as exc:
            await _release_paid_chat_turn(
                chat_turn_accounting,
                paid_operation_id,
                self.event,
            )
            report_exception(
                "agent_unexpected_behavior",
                exception=exc,
                level="warning",
            )
            return await self.event.reply(
                _("I couldn't answer that. Please try again. You weren't charged.")
            )
        except Exception as exc:
            await _release_paid_chat_turn(
                chat_turn_accounting,
                paid_operation_id,
                self.event,
            )
            report_exception("chat_agent_failed", exception=exc)
            return await self.event.reply(
                _("I couldn't answer that. Please try again. You weren't charged.")
            )
