"""Turn Umami JSON into short text an assistant can quote."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from umami_mcp.dates import DateWindow


def as_int(value: Any) -> int | None:
    """Coerce Umami's numbers, numeric strings, and nulls. Booleans are not numbers."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            return None
    if isinstance(value, dict) and "value" in value:
        return as_int(value.get("value"))
    return None


def stat_pair(payload: dict[str, Any], key: str) -> tuple[int, int | None]:
    """Read one stat from either response shape Umami has shipped.

    Current docs return flat numbers plus a `comparison` object. Older v2
    responses nest `{value, prev}` on each stat and always include the previous
    period. Missing numbers become 0; a missing comparison stays None.
    """

    raw = payload.get(key)
    if isinstance(raw, dict) and ("value" in raw or "prev" in raw):
        previous = as_int(raw.get("prev")) if "prev" in raw else None
        return as_int(raw.get("value")) or 0, previous
    comparison = payload.get("comparison")
    previous = None
    if isinstance(comparison, dict) and key in comparison:
        previous = as_int(comparison.get(key))
    return as_int(raw) or 0, previous


def format_stats(
    label: str,
    window: DateWindow,
    timezone: str,
    payload: dict[str, Any],
    *,
    filters: dict[str, str] | None = None,
) -> str:
    pageviews, pageviews_prev = stat_pair(payload, "pageviews")
    visitors, visitors_prev = stat_pair(payload, "visitors")
    visits, visits_prev = stat_pair(payload, "visits")
    bounces, bounces_prev = stat_pair(payload, "bounces")
    totaltime, totaltime_prev = stat_pair(payload, "totaltime")

    bounce_rate = f"{bounces / visits * 100:.1f}% of visits" if visits else "no visits"
    average = format_duration(totaltime // visits) if visits else None
    total_display = format_duration(totaltime)
    if average:
        total_display = f"{total_display}  avg {average}/visit"

    rows = [
        ("pageviews", f"{pageviews:,}", _change(pageviews, pageviews_prev)),
        ("visitors", f"{visitors:,}", _change(visitors, visitors_prev)),
        ("visits", f"{visits:,}", _change(visits, visits_prev)),
        ("bounces", f"{bounces:,}  {bounce_rate}", _change(bounces, bounces_prev)),
        ("total time", total_display, _change(totaltime, totaltime_prev, duration=True)),
    ]
    previous_start, previous_end = window.previous
    lines = [
        f"{label} — {window.label}",
        f"{_stamp(window.start)} → {_stamp(window.end)} ({timezone})",
        f"compared with {_stamp(previous_start)} → {_stamp(previous_end)}",
    ]
    if filters:
        lines.append("filters: " + ", ".join(f"{key}={value}" for key, value in filters.items()))
    lines.append("")
    lines.extend(_align(rows))
    lines.append("")
    lines.append("total time is seconds from Umami, shown here as a duration. avg/visit is total time ÷ visits.")
    return "\n".join(lines)


def format_pageviews(
    label: str,
    window: DateWindow,
    timezone: str,
    unit: str,
    payload: dict[str, Any],
    *,
    filters: dict[str, str] | None = None,
) -> str:
    pageviews = _points(payload.get("pageviews") if isinstance(payload, dict) else payload)
    sessions_raw = []
    if isinstance(payload, dict):
        sessions_raw = payload.get("sessions")
        if sessions_raw is None:
            sessions_raw = payload.get("visits")
    sessions = {bucket: count for bucket, count in _points(sessions_raw)}
    lines = [
        f"{label} — pageviews by {unit}, {window.label}",
        f"{_stamp(window.start)} → {_stamp(window.end)} ({timezone})",
    ]
    if filters:
        lines.append("filters: " + ", ".join(f"{key}={value}" for key, value in filters.items()))
    if not pageviews and not sessions:
        lines.append("")
        lines.append("No pageviews in this range.")
        return "\n".join(lines)

    order = [bucket for bucket, _ in pageviews] or list(sessions)
    for bucket in sessions:
        if bucket not in order:
            order.append(bucket)
    shown = order[-100:]
    omitted = len(order) - len(shown)
    view_map = dict(pageviews)
    rows = [
        (_bucket(bucket, timezone), f"{view_map.get(bucket, 0):,}", f"{sessions.get(bucket, 0):,}")
        for bucket in shown
    ]
    all_views = sum(count for _, count in pageviews)
    all_sessions = sum(sessions.values())
    lines.append("")
    lines.extend(_align([("time", "pageviews", "sessions"), *rows, ("total", f"{all_views:,}", f"{all_sessions:,}")]))
    if omitted:
        lines.append(f"showing the latest {len(shown)} of {len(order)} points. total is the full range.")
    comparison = payload.get("compare") if isinstance(payload, dict) else None
    if isinstance(comparison, dict):
        prev_views = sum(count for _, count in _points(comparison.get("pageviews")))
        prev_sessions = sum(count for _, count in _points(comparison.get("sessions") or comparison.get("visits")))
        lines.append(f"previous period: {prev_views:,} pageviews, {prev_sessions:,} sessions")
    return "\n".join(lines)


def format_metrics(
    label: str,
    window: DateWindow,
    timezone: str,
    metric: str,
    payload: Any,
    *,
    filters: dict[str, str] | None = None,
) -> str:
    rows_raw = _points(payload)
    lines = [
        f"{label} — {metric}, {window.label}",
        f"{_stamp(window.start)} → {_stamp(window.end)} ({timezone})",
    ]
    if filters:
        lines.append("filters: " + ", ".join(f"{key}={value}" for key, value in filters.items()))
    if not rows_raw:
        lines.append("")
        lines.append(f"No {metric} data in this range.")
        return "\n".join(lines)
    rows = [(f"{count:,}", value or "(none)") for value, count in rows_raw]
    lines.append("")
    lines.extend(_align([("count", "value"), *[(count, name) for count, name in rows]]))
    return "\n".join(lines)


def format_active(label: str, visitors: int) -> str:
    noun = "visitor" if visitors == 1 else "visitors"
    return f"{label} — {visitors:,} {noun} active in the last few minutes"


def format_events(
    label: str,
    window: DateWindow,
    timezone: str,
    payload: Any,
    *,
    kind: str,
    filters: dict[str, str] | None = None,
) -> str:
    rows: list[Any]
    count: int | None
    if isinstance(payload, dict):
        data = payload.get("data")
        rows = data if isinstance(data, list) else []
        count = as_int(payload.get("count"))
    elif isinstance(payload, list):
        rows = payload
        count = len(rows)
    else:
        rows = []
        count = 0

    shown = len(rows)
    if count is None:
        summary = f"{shown} {kind}"
    elif count == shown:
        summary = f"{shown} {kind}"
    else:
        summary = f"showing {shown} of {count:,} {kind}"
    lines = [
        f"{label} — {summary}, {window.label}",
        f"{_stamp(window.start)} → {_stamp(window.end)} ({timezone})",
    ]
    if filters:
        lines.append("filters: " + ", ".join(f"{key}={value}" for key, value in filters.items()))
    if not rows:
        lines.append("")
        lines.append("No events in this range.")
        return "\n".join(lines)

    zone = ZoneInfo(timezone)
    lines.append("")
    for row in rows:
        if not isinstance(row, dict):
            continue
        when = _event_time(row.get("createdAt"), zone)
        name = row.get("eventName") or _event_kind(row.get("eventType"))
        path = row.get("urlPath") or ""
        query = row.get("urlQuery") or ""
        if query:
            path = f"{path}?{query}" if path else f"?{query}"
        referrer = row.get("referrerDomain") or ""
        bits = [when, str(name or "event")]
        if path:
            bits.append(path)
        if referrer:
            bits.append(f"via {referrer}")
        lines.append("  ".join(bits))
    return "\n".join(lines)


def format_websites(sites: list[tuple[str, str, str]], *, query: str | None) -> str:
    if not sites:
        if query:
            return f"No website matched {query!r}."
        return "No websites found for this account."
    noun = "website" if len(sites) == 1 else "websites"
    heading = f"{len(sites)} {noun}"
    if query:
        heading += f" matching {query!r}"
    lines = [heading, ""]
    for name, domain, website_id in sites:
        lines.append(name or "(unnamed)")
        lines.append(f"  domain  {domain or '—'}")
        lines.append(f"  id      {website_id}")
    return "\n".join(lines)


def format_duration(seconds: int) -> str:
    remaining = max(0, int(seconds))
    days, remaining = divmod(remaining, 86_400)
    hours, remaining = divmod(remaining, 3_600)
    minutes, secs = divmod(remaining, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes and days == 0:
        parts.append(f"{minutes}m")
    if days == 0 and hours == 0:
        parts.append(f"{secs}s")
    return " ".join(parts) if parts else "0s"


def _change(current: int, previous: int | None, *, duration: bool = False) -> str:
    if previous is None:
        return ""
    if previous == 0:
        if current == 0:
            return "0% vs previous (0)"
        return "previous was 0"
    percent = (current - previous) / previous * 100
    sign = "+" if percent > 0 else ""
    shown = format_duration(previous) if duration else f"{previous:,}"
    return f"{sign}{percent:.1f}% vs previous ({shown})"


def _align(rows: list[tuple[str, ...]]) -> list[str]:
    widths = [max(len(row[index]) for row in rows) for index in range(len(rows[0]))]
    rendered = []
    for row in rows:
        rendered.append("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip())
    return rendered


def _points(payload: Any) -> list[tuple[str, int]]:
    rows = payload
    if isinstance(payload, dict):
        data = payload.get("data")
        rows = data if isinstance(data, list) else []
    if not isinstance(rows, list):
        return []
    points: list[tuple[str, int]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_x = row.get("x")
        points.append(("" if raw_x is None else str(raw_x), as_int(row.get("y")) or 0))
    return points


def _bucket(value: str, timezone: str) -> str:
    if re.fullmatch(r"\d{13}", value):
        moment = datetime.fromtimestamp(int(value) / 1000, ZoneInfo(timezone))
        return moment.strftime("%Y-%m-%d %H:%M")
    if re.fullmatch(r"\d{10}", value):
        moment = datetime.fromtimestamp(int(value), ZoneInfo(timezone))
        return moment.strftime("%Y-%m-%d %H:%M")
    return value.replace("T", " ")[:19]


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M")


def _event_time(value: Any, zone: ZoneInfo) -> str:
    if not isinstance(value, str) or not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value[:16]
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
    return parsed.astimezone(zone).strftime("%Y-%m-%d %H:%M")


def _event_kind(event_type: Any) -> str:
    number = as_int(event_type)
    if number == 1:
        return "pageview"
    if number == 2:
        return "event"
    return "event"
