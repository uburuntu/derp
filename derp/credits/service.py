"""Credit service for checking and deducting credits.

This is the main entry point for credit operations. It handles:
- Tier selection based on credit balance
- Tool access checking with daily limits
- Credit deduction after successful operations
- Credit purchases
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import logfire

from derp.credits.models import (
    ModelTier,
    ModelType,
    get_default_model,
    get_model,
)
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

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# Context limits by tier
CONTEXT_LIMITS: dict[ModelTier, int] = {
    ModelTier.CHEAP: 10,  # Free tier: limited context
    ModelTier.STANDARD: 100,  # Paid tier: full context
    ModelTier.PREMIUM: 100,  # Premium: full context
}


class CreditService:
    """Service for managing credits and access control.

    Usage:
        service = CreditService(session)

        # Get orchestrator config (which model/tier to use)
        tier, model_id, context_limit = await service.get_orchestrator_config(
            user_telegram_id, chat_telegram_id
        )

        # Check tool access
        result = await service.check_tool_access(
            user_id, chat_id, "image_generate"
        )
        if result.allowed:
            # Execute tool
            ...
            # Deduct credits after success
            await service.deduct(result, user_id, chat_id, "image_generate")
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_orchestrator_config(
        self,
        user_telegram_id: int,
        chat_telegram_id: int,
    ) -> tuple[ModelTier, str, int]:
        """Get orchestrator configuration based on credit balance.

        Returns:
            Tuple of (tier, model_id, context_limit).
            - Free tier (no credits): CHEAP model, 10 message context
            - Paid tier (has credits): STANDARD model, 100 message context
        """
        chat_credits, user_credits = await get_balances(
            self.session, user_telegram_id, chat_telegram_id
        )

        if chat_credits > 0 or user_credits > 0:
            model = get_default_model(ModelType.TEXT, ModelTier.STANDARD)
            tier = ModelTier.STANDARD
        else:
            model = get_default_model(ModelType.TEXT, ModelTier.CHEAP)
            tier = ModelTier.CHEAP

        context_limit = CONTEXT_LIMITS[tier]

        logfire.debug(
            "orchestrator_config",
            tier=tier.value,
            model=model.id,
            context_limit=context_limit,
            chat_credits=chat_credits,
            user_credits=user_credits,
        )

        return tier, model.id, context_limit

    async def check_tool_access(
        self,
        user_id: UUID,
        chat_id: UUID,
        user_telegram_id: int,
        chat_telegram_id: int,
        tool_name: str,
        model_id: str | None = None,
    ) -> CreditCheckResult:
        """Check if a tool can be used, considering credits and daily limits.

        The check order:
        1. Free daily limit (if available)
        2. Chat credits
        3. User credits
        4. Reject

        Args:
            user_id: User's database UUID.
            chat_id: Chat's database UUID.
            user_telegram_id: User's Telegram ID (for balance lookup).
            chat_telegram_id: Chat's Telegram ID (for balance lookup).
            tool_name: Name of the tool to check.
            model_id: Optional specific model to use (defaults to tool's default).

        Returns:
            CreditCheckResult with access decision and details.
        """
        tool = get_tool(tool_name)

        # Resolve model
        if model_id:
            model = get_model(model_id)
        elif tool.default_model_id:
            model = get_model(tool.default_model_id)
        else:
            # Use default for the tool's model type
            model = get_default_model(tool.model_type, ModelTier.STANDARD)

        total_cost = tool.total_cost(model.credit_cost)

        # Get balances
        chat_credits, user_credits = await get_balances(
            self.session, user_telegram_id, chat_telegram_id
        )

        # Check free daily limit first
        if tool.free_daily_limit > 0:
            used = await get_daily_usage(self.session, user_id, chat_id, tool_name)
            if used < tool.free_daily_limit:
                return CreditCheckResult(
                    allowed=True,
                    tier=model.tier,
                    model_id=model.id,
                    source="free",
                    credits_to_deduct=0,
                    credits_remaining=None,
                    free_remaining=tool.free_daily_limit - used - 1,
                )

        # Check chat credits
        if chat_credits >= total_cost:
            return CreditCheckResult(
                allowed=True,
                tier=model.tier,
                model_id=model.id,
                source="chat",
                credits_to_deduct=total_cost,
                credits_remaining=chat_credits - total_cost,
                free_remaining=None,
            )

        # Check user credits
        if user_credits >= total_cost:
            return CreditCheckResult(
                allowed=True,
                tier=model.tier,
                model_id=model.id,
                source="user",
                credits_to_deduct=total_cost,
                credits_remaining=user_credits - total_cost,
                free_remaining=None,
            )

        # Rejected
        return CreditCheckResult(
            allowed=False,
            tier=model.tier,
            model_id=model.id,
            source="rejected",
            credits_to_deduct=0,
            credits_remaining=0,
            free_remaining=0,
            reject_reason=f"Need {total_cost} credits for {tool.name}",
        )

    async def deduct(
        self,
        result: CreditCheckResult,
        user_id: UUID,
        chat_id: UUID,
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
            user_id: User's database UUID.
            chat_id: Chat's database UUID.
            tool_name: Name of the tool used.
            idempotency_key: Optional key to prevent duplicate charges.
            metadata: Optional additional context.
        """
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
            await increment_daily_usage(self.session, user_id, chat_id, tool_name)
            logfire.info(
                "free_usage_incremented",
                tool=tool_name,
                user_id=str(user_id),
                chat_id=str(chat_id),
            )
        elif result.source == "chat":
            await deduct_chat_credits(
                self.session,
                chat_id,
                user_id,
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
                chat_id=str(chat_id),
            )
        elif result.source == "user":
            await deduct_user_credits(
                self.session,
                user_id,
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
                user_id=str(user_id),
            )

    async def purchase_credits(
        self,
        user_id: UUID,
        chat_id: UUID | None,
        amount: int,
        telegram_charge_id: str,
        *,
        pack_name: str | None = None,
    ) -> int:
        """Process a credit purchase from Telegram Stars.

        Args:
            user_id: User's database UUID.
            chat_id: Chat's database UUID (None for personal credits).
            amount: Number of credits to add.
            telegram_charge_id: Telegram payment charge ID (for idempotency).

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
                telegram_charge_id=telegram_charge_id,
            )
            # Return the balance from the existing transaction
            return existing.balance_after

        metadata = {"pack_name": pack_name} if pack_name else {}

        if chat_id:
            new_balance = await add_chat_credits(
                self.session,
                chat_id,
                user_id,
                amount,
                "purchase",
                telegram_charge_id=telegram_charge_id,
                idempotency_key=telegram_charge_id,
                metadata=metadata,
            )
            logfire.info(
                "chat_credits_purchased",
                amount=amount,
                chat_id=str(chat_id),
                user_id=str(user_id),
                new_balance=new_balance,
            )
        else:
            new_balance = await add_user_credits(
                self.session,
                user_id,
                amount,
                "purchase",
                telegram_charge_id=telegram_charge_id,
                idempotency_key=telegram_charge_id,
                metadata=metadata,
            )
            logfire.info(
                "user_credits_purchased",
                amount=amount,
                user_id=str(user_id),
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
                "refund_failed_not_found", telegram_charge_id=telegram_charge_id
            )
            return False

        if original.type != "purchase":
            logfire.warn(
                "refund_failed_not_purchase",
                telegram_charge_id=telegram_charge_id,
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
                "refund_already_processed", telegram_charge_id=telegram_charge_id
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
            telegram_charge_id=telegram_charge_id,
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
        f"{reject_reason}. Suggest the user purchase credits with /buy.]"
    )
