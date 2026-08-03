"""Persist and project user-visible AI run information."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from derp.catalog import get_google_model_by_id, get_openrouter_model_by_id
from derp.inference_usage import InferenceTokenUsage
from derp.models import (
    Chat,
    ChatRunReceipt,
    DeliveryIntent,
    InferenceUsage,
    OperationQuote,
    PaidOperation,
    User,
)
from derp.operations import OperationState
from derp.run_info.types import ChatRunDelivery, RunInfo, RunPrivacyMode

type TransactionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class ChatRunReceiptConflictError(RuntimeError):
    """An operation was already bound to different delivery facts."""


class RunInfoService:
    """Own content-free delivery links and derive truthful run receipts."""

    def __init__(self, transactions: TransactionFactory) -> None:
        self._transactions = transactions

    async def record_chat_delivery(self, delivery: ChatRunDelivery) -> None:
        if not isinstance(delivery, ChatRunDelivery):
            raise TypeError("delivery must be a ChatRunDelivery")
        values = {
            "operation_id": delivery.operation_id,
            "chat_id": delivery.chat_id,
            "requester_id": delivery.requester_id,
            "request_message_id": delivery.request_message_id,
            "response_message_ids": list(delivery.response_message_ids),
            "model_key": delivery.model_key,
            "model_display_name": delivery.model_display_name,
            "privacy_mode": delivery.privacy_mode.value,
            "context_messages": delivery.context_messages,
            "context_turns": delivery.context_turns,
            "context_estimated_tokens": delivery.context_estimated_tokens,
        }
        async with self._transactions() as session:
            created = await session.scalar(
                insert(ChatRunReceipt)
                .values(**values)
                .on_conflict_do_nothing(index_elements=[ChatRunReceipt.operation_id])
                .returning(ChatRunReceipt.operation_id)
            )
            if created is not None:
                return
            stored = await session.get(ChatRunReceipt, delivery.operation_id)
            if stored is None or not _delivery_matches(stored, values):
                raise ChatRunReceiptConflictError(
                    "chat operation already has different delivery metadata"
                )

    async def get_for_telegram_message(
        self,
        *,
        telegram_chat_id: int,
        telegram_message_id: int,
        viewer_telegram_id: int,
    ) -> RunInfo | None:
        if isinstance(telegram_chat_id, bool) or not isinstance(telegram_chat_id, int):
            raise TypeError("telegram_chat_id must be an integer")
        if telegram_chat_id == 0:
            raise ValueError("telegram_chat_id must not be zero")
        if (
            isinstance(telegram_message_id, bool)
            or not isinstance(telegram_message_id, int)
            or telegram_message_id <= 0
        ):
            raise ValueError("telegram_message_id must be a positive integer")
        if (
            isinstance(viewer_telegram_id, bool)
            or not isinstance(viewer_telegram_id, int)
            or viewer_telegram_id <= 0
        ):
            raise ValueError("viewer_telegram_id must be a positive integer")

        async with self._transactions() as session:
            chat_row = (
                await session.execute(
                    select(
                        ChatRunReceipt,
                        PaidOperation,
                        OperationQuote,
                        User.telegram_id,
                    )
                    .join(Chat, Chat.id == ChatRunReceipt.chat_id)
                    .join(
                        PaidOperation,
                        PaidOperation.id == ChatRunReceipt.operation_id,
                    )
                    .join(OperationQuote, OperationQuote.id == PaidOperation.quote_id)
                    .join(User, User.id == OperationQuote.requester_id)
                    .where(
                        Chat.telegram_id == telegram_chat_id,
                        ChatRunReceipt.response_message_ids.contains(
                            [telegram_message_id]
                        ),
                    )
                )
            ).one_or_none()
            if chat_row is not None:
                receipt, operation, quote, requester_telegram_id = chat_row
                return await _project(
                    session,
                    operation=operation,
                    quote=quote,
                    model_display_name=receipt.model_display_name,
                    privacy_mode=RunPrivacyMode(receipt.privacy_mode),
                    context_messages=receipt.context_messages,
                    context_turns=receipt.context_turns,
                    context_estimated_tokens=receipt.context_estimated_tokens,
                    charge_visible=requester_telegram_id == viewer_telegram_id,
                )

            media_row = (
                await session.execute(
                    select(
                        DeliveryIntent,
                        PaidOperation,
                        OperationQuote,
                        User.telegram_id,
                    )
                    .join(
                        PaidOperation,
                        PaidOperation.id == DeliveryIntent.operation_id,
                    )
                    .join(OperationQuote, OperationQuote.id == PaidOperation.quote_id)
                    .join(User, User.id == OperationQuote.requester_id)
                    .where(
                        DeliveryIntent.chat_id == telegram_chat_id,
                        DeliveryIntent.telegram_message_ids.contains(
                            [telegram_message_id]
                        ),
                    )
                )
            ).one_or_none()
            if media_row is None:
                return None
            _intent, operation, quote, requester_telegram_id = media_row
            return await _project(
                session,
                operation=operation,
                quote=quote,
                model_display_name=_catalog_display_name(quote),
                privacy_mode=RunPrivacyMode.PRIVATE,
                charge_visible=requester_telegram_id == viewer_telegram_id,
            )


async def _project(
    session: AsyncSession,
    *,
    operation: PaidOperation,
    quote: OperationQuote,
    model_display_name: str,
    privacy_mode: RunPrivacyMode,
    context_messages: int | None = None,
    context_turns: int | None = None,
    context_estimated_tokens: int | None = None,
    charge_visible: bool = True,
) -> RunInfo:
    usage_rows = tuple(
        await session.scalars(
            select(InferenceUsage)
            .where(
                InferenceUsage.operation_id == operation.id,
                InferenceUsage.status == "succeeded",
            )
            .order_by(InferenceUsage.provider_started_at, InferenceUsage.id)
        )
    )
    tokens = _aggregate_tokens(usage_rows)
    actual_model_id = next(
        (row.actual_model_id for row in reversed(usage_rows) if row.actual_model_id),
        None,
    )
    if actual_model_id:
        model_display_name = (
            _known_display_name(
                provider=quote.provider,
                model_id=actual_model_id,
            )
            or model_display_name
        )
    return RunInfo(
        model_display_name=model_display_name,
        privacy_mode=privacy_mode,
        tokens=tokens,
        context_messages=context_messages,
        context_turns=context_turns,
        context_estimated_tokens=context_estimated_tokens,
        charged_credits=_charged_credits(operation, quote),
        charge_visible=charge_visible,
    )


def _aggregate_tokens(rows: tuple[InferenceUsage, ...]) -> InferenceTokenUsage | None:
    available = tuple(row for row in rows if row.usage_available)
    if not available:
        return None
    totals = {
        "input_tokens": sum(row.input_tokens for row in available),
        "output_tokens": sum(row.output_tokens for row in available),
        "cache_read_tokens": sum(row.cache_read_tokens for row in available),
        "cache_write_tokens": sum(row.cache_write_tokens for row in available),
        "reasoning_tokens": sum(row.reasoning_tokens for row in available),
        "audio_input_tokens": sum(row.audio_input_tokens for row in available),
        "audio_output_tokens": sum(row.audio_output_tokens for row in available),
    }
    total_values = tuple(row.total_tokens for row in available)
    return InferenceTokenUsage(
        **totals,
        total_tokens=(
            sum(value for value in total_values if value is not None)
            if all(value is not None for value in total_values)
            else None
        ),
    )


def _charged_credits(operation: PaidOperation, quote: OperationQuote) -> int | None:
    state = OperationState(operation.state)
    if state is OperationState.CAPTURED:
        return quote.amount_credits
    if state in {
        OperationState.RELEASED,
        OperationState.REVERSED,
        OperationState.CANCELED,
        OperationState.FAILED,
    }:
        return 0
    return None


def _catalog_display_name(quote: OperationQuote) -> str:
    return (
        _known_display_name(provider=quote.provider, model_id=quote.provider_model_id)
        or quote.provider_model_id
    )


def _known_display_name(*, provider: str, model_id: str) -> str | None:
    try:
        model = (
            get_openrouter_model_by_id(model_id)
            if provider == "openrouter"
            else get_google_model_by_id(model_id)
        )
    except KeyError:
        return None
    return model.display_name


def _delivery_matches(
    stored: ChatRunReceipt,
    values: dict[str, object],
) -> bool:
    return all(getattr(stored, name) == value for name, value in values.items())


__all__ = ["ChatRunReceiptConflictError", "RunInfoService"]
