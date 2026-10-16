from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from derp.scheduler.jobs import following, next_after, parse_local, show_time
from derp.scheduler.models import Job

BERLIN = ZoneInfo("Europe/Berlin")


def test_daily_keeps_the_wall_clock_across_daylight_saving() -> None:
    before = datetime(2026, 10, 24, 7, 0, tzinfo=UTC)  # 09:00 CEST
    after = following(before, "daily", BERLIN)
    assert after.astimezone(BERLIN).hour == 9 and after == datetime(
        2026, 10, 25, 8, 0, tzinfo=UTC
    )  # 09:00 CET


def test_weekdays_skip_the_weekend_and_months_clamp() -> None:
    friday = datetime(2026, 10, 16, 7, 0, tzinfo=UTC)
    assert following(friday, "weekdays", BERLIN).astimezone(BERLIN).weekday() == 0
    end_of_january = datetime(2027, 1, 31, 12, 0, tzinfo=UTC)
    assert following(end_of_january, "monthly", UTC).date().isoformat() == "2027-02-28"


def test_missed_firings_are_skipped_and_one_offs_finish() -> None:
    start = datetime(2026, 10, 1, 7, 0, tzinfo=UTC)
    job = Job(next_at=start, anchor_at=start, repeat="daily")
    now = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    assert next_after(job, now, BERLIN) == datetime(2026, 10, 17, 7, 0, tzinfo=UTC)
    assert next_after(Job(next_at=now, anchor_at=now, repeat=None), now, BERLIN) is None


def test_spring_forward_and_short_months_return_to_the_original_schedule() -> None:
    first = datetime(2027, 3, 27, 1, 30, tzinfo=UTC)  # 02:30 CET, the night before the clocks jump
    gap = following(first, "daily", BERLIN, first)  # 02:30 doesn't exist on 2027-03-28
    after = following(gap, "daily", BERLIN, first)
    assert after.astimezone(BERLIN).strftime("%m-%d %H:%M") == "03-29 02:30"
    january = datetime(2027, 1, 31, 12, 0, tzinfo=UTC)
    moments = [january]
    for _ in range(3):
        moments.append(following(moments[-1], "monthly", UTC, january))
    assert [m.date().isoformat() for m in moments[1:]] == ["2027-02-28", "2027-03-31", "2027-04-30"]


def test_local_times_and_viewer_local_display() -> None:
    assert parse_local("2026-10-20 09:00", BERLIN) == datetime(2026, 10, 20, 7, 0, tzinfo=UTC)
    assert parse_local("2026-10-20T09:00+00:00", BERLIN) == datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    assert parse_local("tomorrow", BERLIN) is None
    shown = show_time(datetime(2026, 10, 20, 7, 0, tzinfo=UTC), BERLIN)
    assert shown == "![Tue 20 Oct 09:00](tg://time?unix=1792479600&format=wDT)"
