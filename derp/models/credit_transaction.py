"""Credit transaction model for audit trail."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from derp.models.base import Base

if TYPE_CHECKING:
    from derp.models.chat import Chat
    from derp.models.user import User


class CreditTransaction(Base):
    """Audit log for all credit movements.

    Records every credit transaction for:
    - Purchases (Telegram Stars → credits)
    - Spending (tool usage, premium features)
    - Refunds (failed operations, disputes)
    - Gifts (user-to-user transfers)
    - Bonuses (promotional credits)
    """

    __tablename__ = "credit_transactions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # Who performed the action
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)

    # Chat pool affected (NULL for user-only transactions)
    chat_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chats.id"), nullable=True, index=True
    )

    # Transaction type: purchase, spend, refund, gift, bonus, expire
    type: Mapped[str] = mapped_column(String(20), index=True)

    # Amount: positive for credits in, negative for credits out
    amount: Mapped[int] = mapped_column()

    # Running balance after this transaction
    balance_after: Mapped[int] = mapped_column()

    # Context for the transaction
    tool_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # For purchases: links to Telegram payment
    telegram_charge_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # For idempotency: prevent double-charging
    idempotency_key: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True, index=True
    )

    # Additional context (message_id, pack_name, error details, etc.)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)

    # When this transaction occurred
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(tz=__import__("datetime").UTC),
    )

    # Relationships
    user: Mapped[User] = relationship(back_populates="credit_transactions")
    chat: Mapped[Chat | None] = relationship(back_populates="credit_transactions")

    def __repr__(self) -> str:
        return (
            f"<CreditTransaction(type={self.type!r}, amount={self.amount}, "
            f"user_id={self.user_id}, chat_id={self.chat_id})>"
        )
