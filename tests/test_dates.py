from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from umami_mcp.dates import DateRangeError, choose_unit, parse_date_range

NOW = datetime(2026, 11, 9, 7, 30, tzinfo=ZoneInfo("UTC"))
ZONE = "America/New_York"


def _window(text: str, *, now: datetime = NOW, timezone: str = ZONE):
    return parse_date_range(text, timezone=timezone, now=now)


def test_today_uses_local_midnight_through_now() -> None:
    window = _window("today")
    assert window.label == "today"
    assert window.start == datetime(2026, 11, 9, 0, 0, tzinfo=ZoneInfo(ZONE))
    assert window.end == datetime(2026, 11, 9, 2, 30, tzinfo=ZoneInfo(ZONE))
    # EST is UTC-5 in November.
    assert window.start_ms == int(datetime(2026, 11, 9, 5, 0, tzinfo=ZoneInfo("UTC")).timestamp() * 1000)


def test_today_during_daylight_time() -> None:
    summer = datetime(2026, 7, 15, 16, 0, tzinfo=ZoneInfo("UTC"))
    window = _window("today", now=summer)
    assert window.start == datetime(2026, 7, 15, 0, 0, tzinfo=ZoneInfo(ZONE))
    assert window.start_ms == int(datetime(2026, 7, 15, 4, 0, tzinfo=ZoneInfo("UTC")).timestamp() * 1000)


def test_rolling_days_and_hours() -> None:
    week = _window("7d")
    assert week.label == "last 7 days"
    assert week.start == datetime(2026, 11, 2, 2, 30, tzinfo=ZoneInfo(ZONE))
    hours = _window("24h")
    assert hours.label == "last 24 hours"
    assert hours.start == datetime(2026, 11, 8, 2, 30, tzinfo=ZoneInfo(ZONE))
    assert _window("last 30 days").label == "last 30 days"


def test_calendar_periods() -> None:
    month = _window("this month")
    assert month.start == datetime(2026, 11, 1, 0, 0, tzinfo=ZoneInfo(ZONE))
    assert month.end == datetime(2026, 11, 9, 2, 30, tzinfo=ZoneInfo(ZONE))

    previous = _window("last month")
    assert previous.start == datetime(2026, 10, 1, 0, 0, tzinfo=ZoneInfo(ZONE))
    assert previous.end == datetime(2026, 10, 31, 23, 59, 59, 999_000, tzinfo=ZoneInfo(ZONE))

    yesterday = _window("yesterday")
    assert yesterday.start == datetime(2026, 11, 8, 0, 0, tzinfo=ZoneInfo(ZONE))
    assert yesterday.end.day == 8

    # 2026-11-09 is a Monday.
    assert _window("this week").start == datetime(2026, 11, 9, 0, 0, tzinfo=ZoneInfo(ZONE))
    last_week = _window("last week")
    assert last_week.start == datetime(2026, 11, 2, 0, 0, tzinfo=ZoneInfo(ZONE))
    assert last_week.end == datetime(2026, 11, 8, 23, 59, 59, 999_000, tzinfo=ZoneInfo(ZONE))


def test_iso_dates() -> None:
    day = _window("2026-01-15")
    assert day.start == datetime(2026, 1, 15, 0, 0, tzinfo=ZoneInfo(ZONE))
    assert day.end == datetime(2026, 1, 15, 23, 59, 59, 999_000, tzinfo=ZoneInfo(ZONE))

    span = _window("2026-01-01/2026-01-31")
    assert span.start == datetime(2026, 1, 1, 0, 0, tzinfo=ZoneInfo(ZONE))
    assert span.end == datetime(2026, 1, 31, 23, 59, 59, 999_000, tzinfo=ZoneInfo(ZONE))
    assert _window("2026-01-01 to 2026-01-02").label == "2026-01-01 to 2026-01-02"


def test_rejects_unknown_ranges() -> None:
    with pytest.raises(DateRangeError, match="7d"):
        _window("banana")
    with pytest.raises(DateRangeError, match="starts after"):
        _window("2026-02-01/2026-01-01")
    with pytest.raises(DateRangeError, match="timezone"):
        parse_date_range("today", timezone="Not/AZone", now=NOW)


def test_unit_follows_the_span() -> None:
    assert choose_unit(_window("today")) == "hour"
    assert choose_unit(_window("7d")) == "day"
    assert choose_unit(_window("this year")) == "month"
    assert choose_unit(_window("7d"), "hour") == "hour"
    with pytest.raises(DateRangeError):
        choose_unit(_window("7d"), "fortnight")
