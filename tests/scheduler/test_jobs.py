from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from derp.scheduler.jobs import (
    following,
    has_offset,
    keep_wall_time,
    next_after,
    parse_local,
    resume,
    show_time,
)
from derp.scheduler.models import Job

BERLIN = ZoneInfo("Europe/Berlin")
MOSCOW = ZoneInfo("Europe/Moscow")
NEW_YORK = ZoneInfo("America/New_York")


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


def test_daily_keeps_the_wall_clock_across_spring_forward() -> None:
    first = datetime(2027, 3, 27, 8, 0, tzinfo=UTC)  # 09:00 CET
    after = following(first, "daily", BERLIN, first)
    assert after == datetime(2027, 3, 28, 7, 0, tzinfo=UTC)  # 09:00 CEST
    weekly = following(after, "weekly", NEW_YORK, after)  # US clocks already moved: 03:00 EDT stays put
    assert weekly.astimezone(NEW_YORK).strftime("%H:%M") == after.astimezone(NEW_YORK).strftime("%H:%M")


def test_a_new_zone_keeps_repeats_at_their_wall_time_and_leaves_one_offs() -> None:
    now = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
    nine_utc = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)  # 09:00 in a chat set to UTC
    daily = Job(next_at=nine_utc, anchor_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC), repeat="daily")
    keep_wall_time(daily, UTC, MOSCOW, now)
    assert daily.next_at == datetime(2026, 10, 18, 6, 0, tzinfo=UTC)  # 09:00 in Moscow
    assert daily.anchor_at.astimezone(MOSCOW).strftime("%d %H:%M") == "01 09:00"
    once = Job(next_at=nine_utc, anchor_at=nine_utc, repeat=None)
    keep_wall_time(once, UTC, MOSCOW, now)
    assert once.next_at == nine_utc  # an absolute instant

    today = Job(next_at=datetime(2026, 10, 17, 13, 0, tzinfo=UTC), anchor_at=nine_utc, repeat="daily")
    keep_wall_time(today, UTC, MOSCOW, now)  # 09:00 Moscow today has passed: tomorrow, never a replay
    assert today.next_at == datetime(2026, 10, 18, 6, 0, tzinfo=UTC)
    ten_msk = datetime(2026, 10, 1, 7, 0, tzinfo=UTC)
    berlin = Job(next_at=datetime(2026, 10, 24, 7, 0, tzinfo=UTC), anchor_at=ten_msk, repeat="daily")
    keep_wall_time(berlin, MOSCOW, BERLIN, now)  # 10:00 Moscow → 10:00 Berlin, then across the clock change
    assert berlin.next_at.astimezone(BERLIN).hour == 10
    assert following(berlin.next_at, "daily", BERLIN, berlin.anchor_at).astimezone(BERLIN).hour == 10


def test_only_offset_times_skip_the_zone() -> None:
    assert has_offset("2026-10-20T09:00+00:00") and has_offset("2026-10-20 09:00Z")
    assert not has_offset("2026-10-20 09:00") and not has_offset("soon")


def test_resume_moves_a_missed_repeat_on_and_refuses_a_passed_one_off() -> None:
    now = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
    past = datetime(2026, 10, 16, 7, 0, tzinfo=UTC)
    daily = Job(next_at=past, anchor_at=past, repeat="daily", state="paused", misses=3)
    assert resume(daily, now, BERLIN) and daily.state == "active" and daily.misses == 0
    assert daily.next_at == datetime(2026, 10, 18, 7, 0, tzinfo=UTC)
    once = Job(next_at=past, anchor_at=past, repeat=None, state="paused")
    assert not resume(once, now, BERLIN) and once.state == "paused"
