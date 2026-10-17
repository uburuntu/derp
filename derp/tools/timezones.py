"""Time-zone names and conversion for the toolbox (the sandbox has no time zone database)."""

from datetime import UTC, datetime
from functools import cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones


class TimeZoneError(ValueError):
    """A bad zone or time; the message is safe to show the model."""


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name.strip())
    except ZoneInfoNotFoundError, ValueError:
        raise TimeZoneError(f"unknown time zone {name!r} (use IANA names like Europe/Moscow)") from None


@cache
def _names() -> dict[str, str]:
    return {name.lower(): name for name in available_timezones()}


def iana(name: str) -> str:
    """The canonical IANA name (``europe/moscow`` → ``Europe/Moscow``); TimeZoneError if there is none."""
    found = _names().get(name.strip().lower())
    if found is None:
        raise TimeZoneError(f"unknown time zone {name!r} (use IANA names like Europe/Moscow)")
    return found


def convert_time(when: str | None, source: str, targets: list[str]) -> str:
    origin = zone(source)
    if when:
        try:
            moment = datetime.fromisoformat(when.strip().replace(" ", "T", 1))
        except ValueError:
            raise TimeZoneError("time must be ISO, like 2026-10-20 15:30") from None
        moment = moment if moment.tzinfo else moment.replace(tzinfo=origin)
    else:
        moment = datetime.now(UTC)
    lines = [f"{source}: {moment.astimezone(origin):%a %Y-%m-%d %H:%M}"]
    lines += [f"{name}: {moment.astimezone(zone(name)):%a %Y-%m-%d %H:%M}" for name in targets[:10]]
    return "\n".join(lines)
