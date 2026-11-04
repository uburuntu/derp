"""Contributed keys: people's own OpenAI/Anthropic API keys and the chats they power."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, LargeBinary, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from derp.db.base import Base

PROVIDERS = ("openai", "anthropic")
PAUSES = (
    "cap",
    "exhausted",
    "owner",
)  # the monthly cap reached, the provider's credits gone, the owner's own
CHAT_STATES = (
    "offered",
    "on",
    "paused",
    "off",
)  # off: detached (removed, left the chat, turned off by an admin)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} in (" + ", ".join(f"'{v}'" for v in values) + ")"


class ContributedKey(Base):
    """One person's provider key, registered on Derp's OpenRouter account behind its own sub-key.

    Derp never stores the provider key: OpenRouter holds it. The sub-key that reaches it is stored encrypted
    (``keys.vault``). Spend is never stored here: it is summed from ``provider_calls.upstream_usd`` since the
    current cycle began (the cycle restarts monthly on the day the key was added).
    """

    __tablename__ = "contributed_keys"
    __table_args__ = (
        CheckConstraint(_in("provider", PROVIDERS), name="provider"),
        CheckConstraint("paused is null or " + _in("paused", PAUSES), name="paused"),
        CheckConstraint("cap_usd > 0", name="cap_positive"),
        Index("ix_contributed_keys_user_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int]  # the contributor
    provider: Mapped[str] = mapped_column(String(10))
    label: Mapped[str] = mapped_column(String(32))  # OpenRouter's masked form of the key ("sk-...AbCd")
    model: Mapped[str] = mapped_column(String(128))  # the contributor's pick from ``settings.keys.models``
    cap_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2))  # per cycle
    cycle_anchor: Mapped[date]  # cycles start on this day of each month (clamped to short months)
    sub_key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    sub_key: Mapped[bytes] = mapped_column(LargeBinary)  # encrypted (``keys.vault``)
    credential_id: Mapped[str] = mapped_column(String(64))  # OpenRouter's id of the registered provider key
    paused: Mapped[str | None] = mapped_column(String(10))
    paused_until: Mapped[datetime | None]  # cap / exhausted: the next cycle; owner: until resumed
    dead_at: Mapped[datetime | None]  # the provider stopped accepting the key (revoked, suspended)
    noticed_at: Mapped[datetime | None]  # its owner was told about the current pause or failure
    removed_at: Mapped[datetime | None]  # unregistered at OpenRouter; the row stays for the ledger
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class KeyChat(Base):
    """A key plugged into one chat: offered by its owner, turned on by an admin (or the owner, in private)."""

    __tablename__ = "key_chats"
    __table_args__ = (
        CheckConstraint(_in("state", CHAT_STATES), name="state"),
        Index("ix_key_chats_chat_id_state", "chat_id", "state"),
    )

    key_id: Mapped[int] = mapped_column(ForeignKey("contributed_keys.id"), primary_key=True)
    chat_id: Mapped[int] = mapped_column(primary_key=True)
    state: Mapped[str] = mapped_column(String(8), default="offered")
    enabled_by: Mapped[int | None]
    enabled_at: Mapped[datetime | None]  # keys serve a chat in the order they were turned on
    paused_by: Mapped[int | None]  # who paused it here: an owner's own pause only they lift
    card_message_id: Mapped[int | None]  # the enable card or notice in the chat
    refusal_noticed_at: Mapped[datetime | None]  # an explicit refusal shown in the chat (rate-limited)
    paused_noticed_at: Mapped[datetime | None]  # "ran out / stopped working" said in the chat, once per pause
    off_noticed_at: Mapped[datetime | None]  # the owner was told it was detached (left the chat)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class KeyRequest(Base):
    """The step a contributor is at in their private chat: waiting for the pasted key, a cap (a custom one is
    typed), then the model.

    One per person; a plug-in link (``start=key-…``) names the chat the key is for. Expires after
    ``settings.keys.request_minutes``.
    """

    __tablename__ = "key_requests"
    __table_args__ = (CheckConstraint("step in ('paste', 'cap', 'model')", name="step"),)

    user_id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    step: Mapped[str] = mapped_column(String(6))
    chat_id: Mapped[int | None]  # the group the key is for, if any
    key_id: Mapped[int | None] = mapped_column(ForeignKey("contributed_keys.id"))  # the key a custom cap sets
    at: Mapped[datetime] = mapped_column(server_default=func.now())


class KeyModelCheck(Base):
    """The scout's latest look at a curated key model: live (a tools-capable route on its provider) and price."""

    __tablename__ = "key_models"

    model: Mapped[str] = mapped_column(String(128), primary_key=True)
    live: Mapped[bool]
    price_prompt: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))  # USD per 1M tokens
    price_completion: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    price_cache_read: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    checked_at: Mapped[datetime] = mapped_column(server_default=func.now())
    smoked_at: Mapped[datetime | None]  # the last live check that passed (answers Russian, calls a tool)
