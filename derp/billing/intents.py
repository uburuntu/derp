"""Durable purchase-intent creation and strict pre-checkout validation."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from derp.billing.payloads import generate_invoice_payload, hash_invoice_payload
from derp.billing.products import DEFAULT_PRODUCT_CATALOG, ProductCatalog, StarsProduct
from derp.billing.types import (
    ActiveSubscriptionError,
    PreCheckoutDecision,
    PreCheckoutRejection,
    PreCheckoutRequest,
    ProductKind,
    PurchaseIntentHandle,
    PurchaseTarget,
    PurchaseTargetKind,
    UnknownProductError,
)
from derp.legal import TERMS_ACCEPTANCE_VERSION, TermsAcceptanceRequiredError
from derp.models import Chat, LegalAcceptance, PurchaseIntent, Subscription, User

type TransactionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class Clock(Protocol):
    def __call__(self) -> datetime: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class PurchaseIntentService:
    """Own short units of work around opaque invoice terms."""

    def __init__(
        self,
        transactions: TransactionFactory,
        *,
        catalog: ProductCatalog = DEFAULT_PRODUCT_CATALOG,
        clock: Clock = _utc_now,
        intent_ttl: timedelta = timedelta(minutes=15),
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if intent_ttl <= timedelta(0):
            raise ValueError("intent_ttl must be positive")
        self._transactions = transactions
        self._catalog = catalog
        self._clock = clock
        self._intent_ttl = intent_ttl
        self._token_factory = token_factory

    async def create_top_up_intent(
        self,
        *,
        payer_user_id: uuid.UUID,
        target: PurchaseTarget,
        product_id: str,
    ) -> PurchaseIntentHandle:
        """Bind the current top-up version to one payer and wallet target."""
        product = self._catalog.current_top_ups.get(product_id)
        if product is None:
            raise UnknownProductError("Unknown current top-up product")
        return await self._create_intent(
            payer_user_id,
            target,
            product,
            require_terms=True,
        )

    async def create_operator_debug_top_up_intent(
        self,
        *,
        payer_user_id: uuid.UUID,
        target: PurchaseTarget,
    ) -> PurchaseIntentHandle:
        """Create the catalog's hidden one-Star intent for operator diagnostics."""
        return await self._create_intent(
            payer_user_id,
            target,
            self._catalog.debug_top_up,
            require_terms=False,
        )

    async def create_subscription_intent(
        self,
        *,
        payer_user_id: uuid.UUID,
    ) -> PurchaseIntentHandle:
        """Create the only supported personal-plan intent without stacking plans."""
        return await self._create_intent(
            payer_user_id,
            PurchaseTarget.user(payer_user_id),
            self._catalog.subscription_plan,
            require_terms=True,
        )

    async def validate_pre_checkout(
        self,
        request: PreCheckoutRequest,
        *,
        public_intake_enabled: bool = True,
        operator_debug_allowed: bool = False,
    ) -> PreCheckoutDecision:
        """Validate every captured commercial field before approving Telegram."""
        if not isinstance(public_intake_enabled, bool):
            raise TypeError("public_intake_enabled must be a bool")
        if not isinstance(operator_debug_allowed, bool):
            raise TypeError("operator_debug_allowed must be a bool")
        now = self._aware_now()
        token_hash = hash_invoice_payload(request.invoice_payload)
        async with self._transactions() as session:
            intent_reference = (
                await session.execute(
                    select(PurchaseIntent.id, PurchaseIntent.payer_user_id).where(
                        PurchaseIntent.token_hash == token_hash
                    )
                )
            ).one_or_none()
            if intent_reference is None:
                return PreCheckoutDecision(
                    approved=False,
                    rejection=PreCheckoutRejection.UNKNOWN_INTENT,
                )
            payer = await session.scalar(
                select(User)
                .where(User.id == intent_reference.payer_user_id)
                .with_for_update()
            )
            intent = await session.scalar(
                select(PurchaseIntent)
                .where(PurchaseIntent.id == intent_reference.id)
                .with_for_update()
            )
            if intent is None:  # pragma: no cover - purchase intents are not deleted
                return PreCheckoutDecision(
                    approved=False,
                    rejection=PreCheckoutRejection.UNKNOWN_INTENT,
                )
            rejection = await self._validate_checkout_intent(
                session,
                intent,
                payer,
                request,
                now,
                public_intake_enabled=public_intake_enabled,
                operator_debug_allowed=operator_debug_allowed,
            )
            if rejection is not None:
                return PreCheckoutDecision(
                    approved=False,
                    rejection=rejection,
                )
            intent.status = "prechecked"
            return PreCheckoutDecision(approved=True, intent_id=intent.id)

    async def _create_intent(
        self,
        payer_user_id: uuid.UUID,
        target: PurchaseTarget,
        product: StarsProduct,
        *,
        require_terms: bool,
    ) -> PurchaseIntentHandle:
        now = self._aware_now()
        expires_at = now + self._intent_ttl
        payload = generate_invoice_payload(self._token_factory)
        async with self._transactions() as session:
            payer = await session.scalar(
                select(User).where(User.id == payer_user_id).with_for_update()
            )
            if payer is None:
                raise LookupError("Purchase payer does not exist")
            terms_acceptance = await session.scalar(
                select(LegalAcceptance).where(
                    LegalAcceptance.user_id == payer_user_id,
                    LegalAcceptance.document == "terms",
                    LegalAcceptance.version == TERMS_ACCEPTANCE_VERSION,
                )
            )
            if require_terms and terms_acceptance is None:
                raise TermsAcceptanceRequiredError(
                    "current Terms must be accepted before purchase intent creation"
                )
            await self._validate_target(session, payer_user_id, target, product.kind)
            if product.kind is ProductKind.SUBSCRIPTION:
                await self._guard_subscription_intent(session, payer_user_id, now)

            intent = PurchaseIntent(
                token_hash=hash_invoice_payload(payload),
                payer_user_id=payer_user_id,
                terms_acceptance_id=(
                    terms_acceptance.id if terms_acceptance is not None else None
                ),
                target_user_id=(
                    target.id if target.kind is PurchaseTargetKind.USER else None
                ),
                target_chat_id=(
                    target.id if target.kind is PurchaseTargetKind.CHAT else None
                ),
                product_kind=product.kind.value,
                product_id=product.id,
                product_version=product.version,
                credits=product.credits,
                stars=product.stars,
                currency=product.currency,
                subscription_period_seconds=getattr(product, "period_seconds", None),
                status="pending",
                expires_at=expires_at,
                created_at=now,
            )
            session.add(intent)
            await session.flush()

        return PurchaseIntentHandle(
            intent_id=intent.id,
            invoice_payload=payload,
            product_kind=product.kind,
            product_id=product.id,
            product_version=product.version,
            target=target,
            credits=product.credits,
            stars=product.stars,
            currency=product.currency,
            expires_at=expires_at,
            subscription_period_seconds=getattr(product, "period_seconds", None),
        )

    async def _validate_checkout_intent(
        self,
        session: AsyncSession,
        intent: PurchaseIntent,
        payer: User | None,
        request: PreCheckoutRequest,
        now: datetime,
        *,
        public_intake_enabled: bool,
        operator_debug_allowed: bool,
    ) -> PreCheckoutRejection | None:
        if intent.status not in {"pending", "prechecked"}:
            return PreCheckoutRejection.INVALID_STATUS
        if now >= intent.expires_at:
            intent.status = "expired"
            return PreCheckoutRejection.EXPIRED
        if payer is None or payer.telegram_id != request.payer_telegram_id:
            return PreCheckoutRejection.PAYER_MISMATCH
        if request.currency != intent.currency:
            return PreCheckoutRejection.CURRENCY_MISMATCH
        if request.total_amount != intent.stars:
            return PreCheckoutRejection.AMOUNT_MISMATCH
        try:
            kind = ProductKind(intent.product_kind)
            product = self._catalog.resolve(
                kind,
                intent.product_id,
                intent.product_version,
            )
        except ValueError, UnknownProductError:
            return PreCheckoutRejection.PRODUCT_MISMATCH
        if not self._product_matches(intent, product):
            return PreCheckoutRejection.PRODUCT_MISMATCH
        debug = self._catalog.debug_top_up
        is_operator_debug = (
            kind is ProductKind.TOP_UP
            and product.id == debug.id
            and product.version == debug.version
        )
        if not public_intake_enabled and not (
            operator_debug_allowed and is_operator_debug
        ):
            return PreCheckoutRejection.INTAKE_CLOSED
        if not await self._stored_target_is_valid(session, intent, kind):
            return PreCheckoutRejection.TARGET_MISMATCH
        if kind is ProductKind.SUBSCRIPTION:
            subscription = await session.scalar(
                select(Subscription)
                .where(Subscription.user_id == intent.payer_user_id)
                .with_for_update()
            )
            if subscription is not None and subscription.current_period_end > now:
                return PreCheckoutRejection.ACTIVE_SUBSCRIPTION
        return None

    async def _guard_subscription_intent(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        now: datetime,
    ) -> None:
        subscription = await session.scalar(
            select(Subscription)
            .where(Subscription.user_id == user_id)
            .with_for_update()
        )
        if subscription is not None and subscription.current_period_end > now:
            raise ActiveSubscriptionError("User already has a current plan cycle")
        pending = list(
            await session.scalars(
                select(PurchaseIntent)
                .where(
                    PurchaseIntent.payer_user_id == user_id,
                    PurchaseIntent.product_kind == ProductKind.SUBSCRIPTION.value,
                    PurchaseIntent.status.in_(["pending", "prechecked"]),
                )
                .with_for_update()
            )
        )
        for intent in pending:
            intent.status = "canceled"

    @staticmethod
    async def _validate_target(
        session: AsyncSession,
        payer_user_id: uuid.UUID,
        target: PurchaseTarget,
        kind: ProductKind,
    ) -> None:
        if target.kind is PurchaseTargetKind.USER:
            if target.id != payer_user_id:
                raise ValueError("Personal purchases cannot target another user")
            if await session.get(User, target.id) is None:
                raise LookupError("Purchase target user does not exist")
        else:
            if kind is ProductKind.SUBSCRIPTION:
                raise ValueError("Subscriptions cannot target chat wallets")
            if await session.get(Chat, target.id) is None:
                raise LookupError("Purchase target chat does not exist")

    @staticmethod
    async def _stored_target_is_valid(
        session: AsyncSession,
        intent: PurchaseIntent,
        kind: ProductKind,
    ) -> bool:
        if intent.target_user_id is not None:
            return (
                intent.target_chat_id is None
                and intent.target_user_id == intent.payer_user_id
                and await session.get(User, intent.target_user_id) is not None
            )
        return (
            kind is ProductKind.TOP_UP
            and intent.target_chat_id is not None
            and await session.get(Chat, intent.target_chat_id) is not None
        )

    @staticmethod
    def _product_matches(intent: PurchaseIntent, product: StarsProduct) -> bool:
        return (
            intent.product_kind == product.kind.value
            and intent.credits == product.credits
            and intent.stars == product.stars
            and intent.currency == product.currency
            and intent.subscription_period_seconds
            == getattr(product, "period_seconds", None)
        )

    def _aware_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError(
                "PurchaseIntentService clock must return an aware datetime"
            )
        return now


__all__ = ["Clock", "PurchaseIntentService", "TransactionFactory"]
