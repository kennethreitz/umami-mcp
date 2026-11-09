"""Friendly date ranges, resolved in a timezone before they are sent to Umami."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class DateRangeError(Exception):
    """The caller asked for a date range this server does not understand."""


@dataclass(frozen=True)
class DateWindow:
    """Inclusive start and end, both timezone-aware."""

    start: datetime
    end: datetime
    label: str

    @property
    def start_ms(self) -> int:
        return int(self.start.timestamp() * 1000)

    @property
    def end_ms(self) -> int:
        return int(self.end.timestamp() * 1000)

    @property
    def previous(self) -> tuple[datetime, datetime]:
        """Equal-length window immediately before this one.

        That is the comparison Umami's stats endpoint reports (previous period,
        not year-over-year).
        """

        length = self.end - self.start
        previous_end = self.start - timedelta(milliseconds=1)
        return previous_end - length, previous_end


def parse_date_range(text: str, *, timezone: str, now: datetime | None = None) -> DateWindow:
    """Parse ranges such as `today`, `7d`, `30d`, `this month`, or `2026-01-01/2026-01-31`."""

    zone = _zone(timezone)
    current = _now(now, zone)
    raw = text.strip()
    if not raw:
        raise DateRangeError("A date range is required. Try '7d', 'today', or 'this month'.")
    key = re.sub(r"[\s_]+", " ", raw.lower())

    def start_of(day: date) -> datetime:
        return datetime(day.year, day.month, day.day, tzinfo=zone)

    def end_of(day: date) -> datetime:
        return datetime(day.year, day.month, day.day, 23, 59, 59, 999_000, tzinfo=zone)

    today = current.date()

    if key == "today":
        return DateWindow(start_of(today), current, "today")
    if key == "yesterday":
        day = today - timedelta(days=1)
        return DateWindow(start_of(day), end_of(day), "yesterday")
    if key in {"this week", "week"}:
        monday = today - timedelta(days=today.weekday())
        return DateWindow(start_of(monday), current, "this week")
    if key == "last week":
        this_monday = today - timedelta(days=today.weekday())
        last_monday = this_monday - timedelta(days=7)
        last_sunday = this_monday - timedelta(days=1)
        return DateWindow(start_of(last_monday), end_of(last_sunday), "last week")
    if key in {"this month", "month"}:
        return DateWindow(start_of(today.replace(day=1)), current, "this month")
    if key == "last month":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        return DateWindow(start_of(last_prev.replace(day=1)), end_of(last_prev), "last month")
    if key in {"this year", "year"}:
        return DateWindow(start_of(today.replace(month=1, day=1)), current, "this year")
    if key == "last year":
        first_this = today.replace(month=1, day=1)
        last_prev = first_this - timedelta(days=1)
        return DateWindow(start_of(date(last_prev.year, 1, 1)), end_of(last_prev), "last year")

    rolling = re.fullmatch(r"(?:(?:last|past)\s+)?(\d+)\s*(h|hr|hrs|hour|hours|d|day|days)", key)
    if rolling:
        count = int(rolling.group(1))
        if count < 1 or count > 3660:
            raise DateRangeError("Use a range between 1 hour and 3660 days.")
        unit = rolling.group(2)
        if unit.startswith("h"):
            label = "last hour" if count == 1 else f"last {count} hours"
            return DateWindow(current - timedelta(hours=count), current, label)
        label = "last day" if count == 1 else f"last {count} days"
        return DateWindow(current - timedelta(days=count), current, label)

    if re.search(r"\s+to\s+|\.\.|/", raw):
        left, right = re.split(r"\s+to\s+|\.\.|/", raw, maxsplit=1)
        start = _parse_bound(left, zone=zone, is_end=False)
        end = _parse_bound(right, zone=zone, is_end=True)
        if start > end:
            raise DateRangeError(f"Date range starts after it ends: {raw!r}.")
        return DateWindow(start, end, f"{left.strip()} to {right.strip()}")

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[tT ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)?", raw):
        start = _parse_bound(raw, zone=zone, is_end=False)
        end = _parse_bound(raw, zone=zone, is_end=True)
        label = raw[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw) else raw
        return DateWindow(start, end, label)

    raise DateRangeError(
        f"Could not read date range {raw!r}. "
        "Try today, yesterday, 24h, 7d, 30d, 90d, this week, last week, "
        "this month, last month, or ISO dates like 2026-01-01 or 2026-01-01/2026-01-31."
    )


def choose_unit(window: DateWindow, override: str | None = None) -> str:
    """Pick a chart bucket. Short ranges use hours; long ones use months or years."""

    if override:
        unit = override.strip().lower()
        if unit not in {"minute", "hour", "day", "month", "year"}:
            raise DateRangeError("unit must be minute, hour, day, month, or year.")
        return unit
    span = window.end - window.start
    if span <= timedelta(hours=36):
        return "hour"
    if span <= timedelta(days=92):
        return "day"
    if span <= timedelta(days=800):
        return "month"
    return "year"


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise DateRangeError(f"Unknown timezone {name!r}. Use an IANA name such as America/New_York.") from exc


def _now(now: datetime | None, zone: ZoneInfo) -> datetime:
    if now is None:
        return datetime.now(zone)
    if now.tzinfo is None:
        return now.replace(tzinfo=zone)
    return now.astimezone(zone)


def _parse_bound(value: str, *, zone: ZoneInfo, is_end: bool) -> datetime:
    text = value.strip()
    if not text:
        raise DateRangeError("A date range needs both a start and an end.")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        day = date.fromisoformat(text)
        if is_end:
            return datetime(day.year, day.month, day.day, 23, 59, 59, 999_000, tzinfo=zone)
        return datetime(day.year, day.month, day.day, tzinfo=zone)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DateRangeError(f"Could not read date {text!r}. Use YYYY-MM-DD or an ISO date-time.") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)
