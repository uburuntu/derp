"""Credit service for checking and deducting credits.

This is the main entry point for credit operations. It handles:
- Chat model selection based on credit balance
- Tool access checking with daily limits
- Credit deduction after successful operations
- Credit purchases
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

import logfire

from derp.catalog import GoogleModelKey
from derp.credits.tools import TOOL_REGISTRY, get_tool
from derp.credits.types import CreditCheckResult
from derp.db.credits import (
    add_chat_credits,
    add_user_credits,
    deduct_chat_credits,
    deduct_user_credits,
    get_balances,
    get_daily_usage,
    get_transaction_by_idempotency_key,
    increment_daily_usage,
)
from derp.execution import ExecutionPlan, Feature, plan_execution
from derp.history.service import HISTORY_WINDOWS, HistoryWindow
from derp.observability import telemetry_fingerprint

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from derp.models import Chat, User


class CreditService:
    """Service for managing credits and access control.

    Methods accept database models (User, Chat) directly for cleaner API.

    Usage:
        service = CreditService(session)

        # Get the exact orchestrator plan and context policy.
        plan, history_window = await service.get_orchestrator_config(user, chat)

        # Check tool access
        result = await service.check_tool_access(user, chat, "image_generate")
        if result.allowed:
            # Execute tool
            ...
            # Deduct credits after success
            await service.deduct(result, user, chat, "image_generate")
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_orchestrator_config(
        self,
        user: User,
        chat: Chat,
    ) -> tuple[ExecutionPlan, HistoryWindow]:
        """Get orchestrator configuration based on credit balance.

        Args:
            user: Database User model.
            chat: Database Chat model.

        Returns:
            Validated chat execution plan plus its product history window.
        """
        chat_credits, user_credits = await get_balances(
            self.session, user.telegram_id, chat.telegram_id
        )

        if chat_credits > 0 or user_credits > 0:
            model_key = GoogleModelKey.CHAT_STANDARD
        else:
            model_key = GoogleModelKey.CHAT_ECONOMY

        plan = plan_execution(Feature.CHAT, model_key)
        history_window = HISTORY_WINDOWS[plan.model.key]

        logfire.debug(
            "orchestrator_config",
            model_key=plan.model.key.value,
            model=plan.model.provider_model_id,
            history_max_turns=history_window.max_turns,
            history_max_tokens=history_window.max_tokens,
            chat_credits=chat_credits,
            user_credits=user_credits,
        )

        return plan, history_window

    async def check_tool_access(
        self,
        user: User,
        chat: Chat,
        tool_name: str,
        *,
        arguments: Mapping[str, object] | None = None,
    ) -> CreditCheckResult:
        """Check if a tool can be used, considering credits and daily limits.

        The check order:
        1. Free daily limit (if available)
        2. Chat credits
        3. User credits
        4. Reject

        Args:
            user: Database User model.
            chat: Database Chat model.
            tool_name: Name of the tool to check.
            arguments: Validated tool arguments used for model variants.

        Returns:
            CreditCheckResult with access decision and details.
        """
        tool = get_tool(tool_name)
        resolved_arguments = arguments or {}
        plan = tool.resolve_plan(resolved_arguments)
        total_cost = tool.total_cost(tool.model_credit_cost(plan, resolved_arguments))

        # Get balances
        chat_credits, user_credits = await get_balances(
            self.session, user.telegram_id, chat.telegram_id
        )

        # Check free daily limit first
        if tool.free_daily_limit > 0:
            used = await get_daily_usage(self.session, user.id, chat.id, tool_name)
            if used < tool.free_daily_limit:
                return CreditCheckResult(
                    allowed=True,
                    plan=plan,
                    source="free",
                    credits_to_deduct=0,
                    credits_remaining=None,
                    free_remaining=tool.free_daily_limit - used - 1,
                )

        if total_cost == 0:
            return CreditCheckResult(
                allowed=False,
                plan=plan,
                source="rejected",
                credits_to_deduct=0,
                credits_remaining=None,
                free_remaining=0,
                reject_reason=f"Daily limit reached for {tool.name}",
            )

        # Check chat credits
        if chat_credits >= total_cost:
            return CreditCheckResult(
                allowed=True,
                plan=plan,
                source="chat",
                credits_to_deduct=total_cost,
                credits_remaining=chat_credits - total_cost,
                free_remaining=None,
            )

        # Check user credits
        if user_credits >= total_cost:
            return CreditCheckResult(
                allowed=True,
                plan=plan,
                source="user",
                credits_to_deduct=total_cost,
                credits_remaining=user_credits - total_cost,
                free_remaining=None,
            )

        # Rejected
        return CreditCheckResult(
            allowed=False,
            plan=plan,
            source="rejected",
            credits_to_deduct=0,
            credits_remaining=0,
            free_remaining=0,
            reject_reason=f"Need {total_cost} credits for {tool.name}",
        )

    async def deduct(
        self,
        result: CreditCheckResult,
        user: User,
        chat: Chat,
        tool_name: str,
        *,
        idempotency_key: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Deduct credits after successful tool execution.

        Call this ONLY after the tool has successfully completed.
        Uses idempotency_key to prevent double-charging on retries.

        Args:
            result: The CreditCheckResult from check_tool_access.
            user: Database User model.
            chat: Database Chat model.
            tool_name: Name of the tool used.
            idempotency_key: Optional key to prevent duplicate charges.
            metadata: Optional additional context.
        """
        if not result.allowed:
            raise ValueError("Cannot deduct a rejected credit check")
        tool = get_tool(tool_name)
        if (result.plan is None) != (tool.feature is None):
            raise ValueError(f"{tool_name} received an incompatible execution plan")
        if result.plan and result.plan.feature is not tool.feature:
            raise ValueError(
                f"{result.plan.feature.value} cannot settle as {tool_name}"
            )

        # Check idempotency
        if idempotency_key:
            existing = await get_transaction_by_idempotency_key(
                self.session, idempotency_key
            )
            if existing:
                logfire.info(
                    "deduction_skipped_idempotent",
                    idempotency_key=idempotency_key,
                    tool=tool_name,
                )
                return

        if result.source == "free":
            await increment_daily_usage(self.session, user.id, chat.id, tool_name)
            logfire.info(
                "free_usage_incremented",
                tool=tool_name,
                user_id=str(user.id),
                chat_id=str(chat.id),
            )
        elif result.source == "chat":
            await deduct_chat_credits(
                self.session,
                chat.id,
                user.id,
                result.credits_to_deduct,
                tool_name,
                result.model_id,
                idempotency_key=idempotency_key,
                metadata=metadata,
            )
            logfire.info(
                "chat_credits_deducted",
                amount=result.credits_to_deduct,
                tool=tool_name,
                chat_id=str(chat.id),
            )
        elif result.source == "user":
            await deduct_user_credits(
                self.session,
                user.id,
                result.credits_to_deduct,
                tool_name,
                result.model_id,
                idempotency_key=idempotency_key,
                metadata=metadata,
            )
            logfire.info(
                "user_credits_deducted",
                amount=result.credits_to_deduct,
                tool=tool_name,
                user_id=str(user.id),
            )

    async def purchase_credits(
        self,
        user: User,
        chat: Chat | None,
        amount: int,
        telegram_charge_id: str,
        *,
        pack_name: str | None = None,
    ) -> int:
        """Process a credit purchase from Telegram Stars.

        Args:
            user: Database User model.
            chat: Database Chat model (None for personal credits).
            amount: Number of credits to add.
            telegram_charge_id: Telegram payment charge ID (for idempotency).
            pack_name: Optional pack name for metadata.

        Returns:
            New balance after purchase.
        """
        # Use telegram_charge_id as idempotency key
        existing = await get_transaction_by_idempotency_key(
            self.session, telegram_charge_id
        )
        if existing:
            logfire.info(
                "purchase_skipped_duplicate",
                charge_fingerprint=telemetry_fingerprint(telegram_charge_id),
            )
            # Return the balance from the existing transaction
            return existing.balance_after

        metadata = {"pack_name": pack_name} if pack_name else {}

        if chat:
            new_balance = await add_chat_credits(
                self.session,
                chat.id,
                user.id,
                amount,
                "purchase",
                telegram_charge_id=telegram_charge_id,
                idempotency_key=telegram_charge_id,
                metadata=metadata,
            )
            logfire.info(
                "chat_credits_purchased",
                amount=amount,
                chat_id=str(chat.id),
                user_id=str(user.id),
                new_balance=new_balance,
            )
        else:
            new_balance = await add_user_credits(
                self.session,
                user.id,
                amount,
                "purchase",
                telegram_charge_id=telegram_charge_id,
                idempotency_key=telegram_charge_id,
                metadata=metadata,
            )
            logfire.info(
                "user_credits_purchased",
                amount=amount,
                user_id=str(user.id),
                new_balance=new_balance,
            )

        return new_balance

    async def refund_credits(
        self,
        telegram_charge_id: str,
    ) -> bool:
        """Process a refund for a previous purchase.

        Finds the original transaction and reverses it.

        Args:
            telegram_charge_id: The original purchase's charge ID.

        Returns:
            True if refund was processed, False if original not found.
        """
        original = await get_transaction_by_idempotency_key(
            self.session, telegram_charge_id
        )
        if not original:
            logfire.warn(
                "refund_failed_not_found",
                charge_fingerprint=telemetry_fingerprint(telegram_charge_id),
            )
            return False

        if original.type != "purchase":
            logfire.warn(
                "refund_failed_not_purchase",
                charge_fingerprint=telemetry_fingerprint(telegram_charge_id),
                type=original.type,
            )
            return False

        # Reverse the transaction
        refund_key = f"refund:{telegram_charge_id}"
        existing_refund = await get_transaction_by_idempotency_key(
            self.session, refund_key
        )
        if existing_refund:
            logfire.info(
                "refund_already_processed",
                charge_fingerprint=telemetry_fingerprint(telegram_charge_id),
            )
            return True

        if original.chat_id:
            await add_chat_credits(
                self.session,
                original.chat_id,
                original.user_id,
                -original.amount,  # Negative to remove
                "refund",
                idempotency_key=refund_key,
                metadata={"original_charge_id": telegram_charge_id},
            )
        else:
            await add_user_credits(
                self.session,
                original.user_id,
                -original.amount,
                "refund",
                idempotency_key=refund_key,
                metadata={"original_charge_id": telegram_charge_id},
            )

        logfire.info(
            "refund_processed",
            charge_fingerprint=telemetry_fingerprint(telegram_charge_id),
            amount=original.amount,
        )
        return True


def get_placeholder_message(tool_name: str, reject_reason: str) -> str:
    """Generate a placeholder message for tools that can't be used.

    This message is returned to the agent so it can naturally respond
    to the user about the limitation.
    """
    tool = TOOL_REGISTRY.get(tool_name)
    if not tool:
        return f"[TOOL_UNAVAILABLE: {reject_reason}]"

    return (
        f"[TOOL_UNAVAILABLE: {tool.description} requires credits. "
        f"{reject_reason}. Do not suggest a purchase; credit purchases are unavailable.]"
    )
