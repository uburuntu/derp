"""Declarative base shared by every domain package's models."""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, MetaData
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    # Telegram IDs exceed 32 bits; timestamps are always timezone-aware.
    type_annotation_map = {  # noqa: RUF012 - SQLAlchemy reads this class attribute
        int: BigInteger,
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
    }
