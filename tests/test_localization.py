"""Locale-aware display formatting follows aiogram's active locale."""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from aiogram.utils.i18n import I18n

from derp.common.localization import (
    format_local_date,
    format_local_integer,
    format_local_month_day,
    format_local_utc_datetime,
)


@pytest.mark.parametrize(
    ("locale", "full_date", "month_day", "utc_datetime"),
    [
        ("en", "19 Aug 2026", "19 Aug", "19 Aug 2026, 09:34 UTC"),
        ("ru", "19 авг. 2026", "19 авг.", "19 авг. 2026, 09:34 UTC"),
    ],
)
def test_formatters_use_active_aiogram_locale(
    setup_i18n: I18n,
    locale: str,
    full_date: str,
    month_day: str,
    utc_datetime: str,
) -> None:
    calendar_date = date(2026, 8, 19)
    instant = datetime(
        2026,
        8,
        19,
        12,
        34,
        tzinfo=timezone(timedelta(hours=3)),
    )

    with setup_i18n.use_locale(locale):
        assert format_local_date(calendar_date) == full_date
        assert format_local_month_day(calendar_date) == month_day
        assert format_local_utc_datetime(instant) == utc_datetime


def test_utc_datetime_rejects_naive_values() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        format_local_utc_datetime(datetime(2026, 8, 19))


def test_utc_datetime_keeps_utc_values() -> None:
    assert format_local_utc_datetime(datetime(2026, 8, 19, tzinfo=UTC)).endswith(
        "00:00 UTC"
    )


@pytest.mark.parametrize(
    ("locale", "expected"),
    [("en", "1,234,567"), ("ru", "1\xa0234\xa0567")],
)
def test_integer_uses_locale_grouping(
    setup_i18n: I18n,
    locale: str,
    expected: str,
) -> None:
    with setup_i18n.use_locale(locale):
        assert format_local_integer(1_234_567) == expected


@pytest.mark.parametrize("value", [True, 1.5, "1"])
def test_integer_rejects_non_integer_values(value: object) -> None:
    with pytest.raises(TypeError, match="integer"):
        format_local_integer(value)  # type: ignore[arg-type]
