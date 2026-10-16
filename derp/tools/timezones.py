"""Time-zone conversion for the toolbox (the sandbox has no time zone database)."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class TimeZoneError(ValueError):
    """A bad zone or time; the message is safe to show the model."""


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name.strip())
    except ZoneInfoNotFoundError, ValueError:
        raise TimeZoneError(f"unknown time zone {name!r} (use IANA names like Europe/Moscow)") from None


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
