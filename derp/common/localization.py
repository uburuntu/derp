"""Locale-aware display formatting shared by Telegram surfaces."""

from __future__ import annotations

from datetime import UTC, date, datetime

from aiogram.utils.i18n import get_i18n
from babel.dates import format_date as babel_format_date
from babel.dates import format_datetime as babel_format_datetime
from babel.numbers import format_decimal as babel_format_decimal

_DATE_FORMAT = "d MMM y"
_MONTH_DAY_FORMAT = "d MMM"
_UTC_DATETIME_FORMAT = "d MMM y, HH:mm z"


def format_local_date(value: date | datetime) -> str:
    """Format a calendar date in the active aiogram locale."""
    return babel_format_date(
        value,
        format=_DATE_FORMAT,
        locale=get_i18n().current_locale,
    )


def format_local_month_day(value: date | datetime) -> str:
    """Format a compact month and day in the active aiogram locale."""
    return babel_format_date(
        value,
        format=_MONTH_DAY_FORMAT,
        locale=get_i18n().current_locale,
    )


def format_local_utc_datetime(value: datetime) -> str:
    """Format an aware datetime in UTC and the active aiogram locale."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("value must be timezone-aware")
    return babel_format_datetime(
        value,
        format=_UTC_DATETIME_FORMAT,
        tzinfo=UTC,
        locale=get_i18n().current_locale,
    )


def format_local_integer(value: int) -> str:
    """Format an integer with the active locale's grouping separator."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("value must be an integer")
    return babel_format_decimal(
        value,
        format="#,##0",
        decimal_quantization=False,
        locale=get_i18n().current_locale,
    )


__all__ = [
    "format_local_date",
    "format_local_integer",
    "format_local_month_day",
    "format_local_utc_datetime",
]
