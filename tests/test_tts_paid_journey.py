"""A paid TTS request crosses every durable boundary exactly once."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from derp.approvals import (
    ApprovalCapability,
    ApprovalTokenCodec,
    DeferredToolApprovalService,
    ResumeLease,
    ResumeUnavailable,
    ResumeUnavailableReason,
)
from derp.approvals.paid_media import (
    PaidMediaApprovalCoordinator,
    PaidMediaRunContext,
)
from derp.artifacts import FilesystemArtifactStore
from derp.delivery import (
    DeliveryMedia,
    DeliveryService,
    DeliveryState,
    ResendTokenCodec,
    TelegramMediaKind,
)
from derp.execution import Succeeded
from derp.features import TtsFeatureService, TtsProviderOutput, TtsRequest
from derp.features.paid_media_operation import (
    PaidMediaDelivered,
    PaidMediaOperationCoordinator,
)
from derp.features.tts_operation import (
    TtsPaidMediaAdapter,
    tts_command_history,
    tts_command_tool_call,
)
from derp.models import Chat, User, Wallet, WalletLot
from derp.operations import (
    OperationLedger,
    OperationRequestBinder,
    OperationState,
    QuoteEngine,
    WalletOwner,
    WalletOwnerKind,
)

pytestmark = pytest.mark.database


async def test_paid_tts_journey_is_approved_delivered_and_replay_safe(
    db_engine: AsyncEngine,
    tmp_path,
) -> None:
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)

    @asynccontextmanager
    async def transactions() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session, session.begin():
            yield session

    now = datetime(2026, 7, 21, 10, tzinfo=UTC)
    telegram_user_id = 10_000_000 + uuid4().int % 9_000_000_000
    async with transactions() as session:
        user = User(
            telegram_id=telegram_user_id,
            is_bot=False,
            first_name="Voice",
        )
        chat = Chat(telegram_id=telegram_user_id, type="private")
        session.add_all((user, chat))
        await session.flush()
        requester_id = user.id
        chat_id = chat.id
        wallet = Wallet(user_id=requester_id)
        session.add(wallet)
        await session.flush()
        session.add(
            WalletLot(
                wallet_id=wallet.id,
                kind="purchased",
                granted_credits=1_000,
                available_credits=1_000,
            )
        )

    def clock() -> datetime:
        return now

    secret = b"tts-paid-journey-secret".ljust(32, b"!")
    ledger = OperationLedger(transactions, clock=clock)
    bot = MagicMock(spec=Bot)
    bot.send_voice = AsyncMock(return_value=SimpleNamespace(message_id=501))
    delivery = DeliveryService(
        transactions,
        FilesystemArtifactStore(tmp_path / "artifacts"),
        bot,
        ledger,
        ResendTokenCodec(secret),
        clock=clock,
        artifact_ttl=timedelta(hours=1),
    )
    operations = PaidMediaOperationCoordinator(
        ledger,
        QuoteEngine(),
        delivery,
        OperationRequestBinder(secret),
        clock=clock,
    )
    approvals = DeferredToolApprovalService(
        transactions,
        ApprovalTokenCodec(secret),
        clock=clock,
    )
    workflow = PaidMediaApprovalCoordinator(
        operations,
        approvals,
        ledger,
        OperationRequestBinder(secret),
    )
    provider = MagicMock()
    provider.synthesize = AsyncMock(
        return_value=Succeeded(
            TtsProviderOutput(
                DeliveryMedia(
                    TelegramMediaKind.VOICE,
                    "audio/ogg",
                    b"durable voice",
                ),
                duration_seconds=1.0,
            )
        )
    )
    adapter = TtsPaidMediaAdapter(TtsFeatureService(provider))
    request = TtsRequest("A durable hello", 30)
    tool_call = tts_command_tool_call(request)
    context = PaidMediaRunContext(
        requester_id=requester_id,
        requester_telegram_id=telegram_user_id,
        chat_id=chat_id,
        chat_telegram_id=telegram_user_id,
        message_id=42,
        thread_id=None,
        business_connection_id=None,
    )

    prepared = await workflow.prepare(
        context=context,
        tool_call=tool_call,
        original_history=tts_command_history(request, tool_call),
        adapter=adapter,
    )
    provider.synthesize.assert_not_awaited()
    capability = ApprovalCapability(
        prepared.handle.callback_token,
        requester_telegram_id=telegram_user_id,
        chat_telegram_id=telegram_user_id,
        thread_id=None,
    )
    approval = await workflow.approve_and_claim(capability)
    assert isinstance(approval.claim, ResumeLease)

    resumed = await workflow.resume(
        lease=approval.claim,
        context=context,
        adapter=adapter,
    )
    assert resumed.outcome == PaidMediaDelivered(
        prepared.quote.operation_id,
        (501,),
    )
    await workflow.complete(resumed.lease)

    provider.synthesize.assert_awaited_once()
    bot.send_voice.assert_awaited_once()
    snapshot = await ledger.get_snapshot(prepared.quote.operation_id)
    assert snapshot.state is OperationState.CAPTURED
    assert snapshot.delivery_state is DeliveryState.DELIVERED
    balance = await ledger.balance(WalletOwner(WalletOwnerKind.USER, requester_id))
    assert balance.purchased_available == 1_000 - prepared.quote.credits

    duplicate = await workflow.approve_and_claim(capability)
    assert isinstance(duplicate.claim, ResumeUnavailable)
    assert duplicate.claim.reason is ResumeUnavailableReason.RESUMED
    provider.synthesize.assert_awaited_once()
    bot.send_voice.assert_awaited_once()
