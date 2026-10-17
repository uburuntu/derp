import pytest

from derp.tools.timezones import TimeZoneError, convert_time, iana


def test_time_conversion_uses_iana_zones() -> None:
    text = convert_time("2026-10-20 15:30", "Europe/Moscow", ["Asia/Tokyo", "UTC"])
    assert text.splitlines() == [
        "Europe/Moscow: Tue 2026-10-20 15:30",
        "Asia/Tokyo: Tue 2026-10-20 21:30",
        "UTC: Tue 2026-10-20 12:30",
    ]
    with pytest.raises(TimeZoneError):
        convert_time(None, "Mars/Olympus", [])


def test_zone_names_are_canonical_iana() -> None:
    assert iana(" europe/moscow ") == "Europe/Moscow"
    assert iana("UTC") == "UTC"
    with pytest.raises(TimeZoneError):
        iana("Moscow")
    with pytest.raises(TimeZoneError):
        iana("../etc/passwd")
