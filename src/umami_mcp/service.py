"""Read operations, with websites addressed by name, domain, or id."""

from __future__ import annotations

import re
from collections.abc import Mapping

from umami_mcp.client import UmamiClient, UmamiError, Website
from umami_mcp.dates import DateRangeError, DateWindow, choose_unit, parse_date_range
from umami_mcp.formatting import (
    format_active,
    format_events,
    format_metrics,
    format_pageviews,
    format_stats,
    format_websites,
)

# Current docs.umami.is metric `type` values. `url` is the older v2 name for `path`.
METRIC_TYPES = (
    "path",
    "entry",
    "exit",
    "title",
    "query",
    "hostname",
    "referrer",
    "domain",
    "channel",
    "event",
    "tag",
    "browser",
    "os",
    "device",
    "screen",
    "language",
    "country",
    "region",
    "city",
    "distinctId",
    "utmSource",
    "utmMedium",
    "utmCampaign",
    "utmContent",
    "utmTerm",
)

_METRIC_ALIASES = {
    "url": "path",
    "urls": "path",
    "page": "path",
    "pages": "path",
    "referrers": "referrer",
    "browsers": "browser",
    "devices": "device",
    "countries": "country",
    "country code": "country",
    "events": "event",
    "operating system": "os",
    "operating-system": "os",
    "host": "hostname",
    "distinct_id": "distinctId",
    "utm_source": "utmSource",
    "utm_medium": "utmMedium",
    "utm_campaign": "utmCampaign",
    "utm_content": "utmContent",
    "utm_term": "utmTerm",
}

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

_EVENT_KINDS = {
    "event": 2,
    "events": 2,
    "custom": 2,
    "pageview": 1,
    "pageviews": 1,
    "all": None,
}


class Analytics:
    def __init__(self, client: UmamiClient) -> None:
        self.client = client

    async def list_websites(self, query: str | None = None) -> str:
        sites = await self.client.list_websites()
        matched = _filter_websites(sites, query)
        rows = [(site.name, site.domain, site.id) for site in _sorted(matched)]
        return format_websites(rows, query=query.strip() if query else None)

    async def stats(
        self,
        website: str,
        date_range: str = "7d",
        timezone: str | None = None,
        **filters: str | None,
    ) -> str:
        zone = self._timezone(timezone)
        window = _window(date_range, zone)
        site = await self._resolve(website)
        chosen = _clean_filters(filters)
        payload = await self.client.stats(site.id, window, timezone=zone, filters=chosen)
        return format_stats(_label(site), window, zone, payload, filters=chosen)

    async def pageviews(
        self,
        website: str,
        date_range: str = "7d",
        timezone: str | None = None,
        unit: str | None = None,
        **filters: str | None,
    ) -> str:
        zone = self._timezone(timezone)
        window = _window(date_range, zone)
        bucket = choose_unit(window, unit)
        site = await self._resolve(website)
        chosen = _clean_filters(filters)
        payload = await self.client.pageviews(site.id, window, timezone=zone, unit=bucket, filters=chosen)
        return format_pageviews(_label(site), window, zone, bucket, payload, filters=chosen)

    async def metrics(
        self,
        website: str,
        metric: str = "path",
        date_range: str = "7d",
        timezone: str | None = None,
        limit: int = 15,
        **filters: str | None,
    ) -> str:
        zone = self._timezone(timezone)
        window = _window(date_range, zone)
        site = await self._resolve(website)
        kind = _metric_type(metric)
        chosen = _clean_filters(filters)
        payload = await self.client.metrics(
            site.id,
            window,
            timezone=zone,
            metric=kind,
            limit=_limit(limit),
            filters=chosen,
        )
        return format_metrics(_label(site), window, zone, kind, payload, filters=chosen)

    async def active(self, website: str) -> str:
        site = await self._resolve(website)
        visitors = await self.client.active(site.id)
        return format_active(_label(site), visitors)

    async def events(
        self,
        website: str,
        date_range: str = "7d",
        timezone: str | None = None,
        event: str | None = None,
        search: str | None = None,
        limit: int = 20,
        kind: str = "event",
    ) -> str:
        zone = self._timezone(timezone)
        window = _window(date_range, zone)
        site = await self._resolve(website)
        event_type, kind_label = _event_kind(kind)
        filters: dict[str, str] = {}
        if event:
            filters["event"] = event
        if search:
            filters["search"] = search
        if event_type is not None:
            filters["eventType"] = str(event_type)
        payload = await self.client.events(
            site.id,
            window,
            timezone=zone,
            limit=_limit(limit, default=20),
            event=event,
            search=search,
            event_type=event_type,
        )
        return format_events(_label(site), window, zone, payload, kind=kind_label, filters=filters or None)

    async def _resolve(self, website: str) -> Website:
        raw = website.strip()
        if not raw:
            raise UmamiError("Pass a website id, name, or domain.")
        sites = await self.client.list_websites()
        matches = _match(sites, raw)
        if len(matches) == 1:
            return matches[0]
        if not matches and _UUID.fullmatch(raw):
            return Website(id=raw, name=raw, domain="")
        if not matches:
            available = ", ".join(f"{site.name} ({site.domain})" for site in _sorted(sites)[:12]) or "none"
            raise UmamiError(f"No website matched {raw!r}. Available: {available}")
        lines = "\n".join(f"- {site.name} ({site.domain}) id={site.id}" for site in matches[:12])
        extra = "" if len(matches) <= 12 else f"\n- … {len(matches) - 12} more"
        raise UmamiError(f"Several websites matched {raw!r}. Pass an id:\n{lines}{extra}")

    def _timezone(self, timezone: str | None) -> str:
        chosen = (timezone or "").strip() or self.client.settings.timezone
        return chosen


def _window(date_range: str, timezone: str) -> DateWindow:
    try:
        return parse_date_range(date_range, timezone=timezone)
    except DateRangeError as exc:
        raise UmamiError(str(exc)) from exc


def _limit(limit: int, *, default: int = 15) -> int:
    if limit is None:
        return default
    if limit < 1 or limit > 100:
        raise UmamiError("limit must be between 1 and 100.")
    return limit


def _clean_filters(filters: Mapping[str, str | None]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, value in filters.items():
        if value is None:
            continue
        text = str(value).strip()
        if text:
            cleaned[key] = text
    return cleaned


def _metric_type(metric: str) -> str:
    text = metric.strip()
    if not text:
        raise UmamiError("A metric type is required, for example path, referrer, browser, country, or event.")
    key = text.lower().replace("_", " ") if " " in text or text.lower() in _METRIC_ALIASES else text
    alias = _METRIC_ALIASES.get(text.lower()) or _METRIC_ALIASES.get(key)
    if alias:
        return alias
    for known in METRIC_TYPES:
        if known.lower() == text.lower():
            return known
    known = ", ".join(METRIC_TYPES)
    raise UmamiError(f"Unknown metric {text!r}. Use one of: {known}.")


def _event_kind(kind: str) -> tuple[int | None, str]:
    key = kind.strip().lower()
    if key not in _EVENT_KINDS:
        raise UmamiError("kind must be event, pageview, or all.")
    event_type = _EVENT_KINDS[key]
    if event_type == 1:
        return 1, "pageviews"
    if event_type == 2:
        return 2, "custom events"
    return None, "events"


def _filter_websites(sites: list[Website], query: str | None) -> list[Website]:
    if query is None or not query.strip():
        return sites
    return _match(sites, query)


def _match(sites: list[Website], query: str) -> list[Website]:
    raw = query.strip()
    if _UUID.fullmatch(raw):
        return [site for site in sites if site.id.lower() == raw.lower()]
    name_key = raw.lower()
    domain_key = _domain(raw)
    exact = [
        site
        for site in sites
        if site.name.lower() == name_key or (domain_key and _domain(site.domain) == domain_key)
    ]
    if exact:
        return exact
    return [
        site
        for site in sites
        if name_key in site.name.lower() or (domain_key and domain_key in _domain(site.domain))
    ]


def _domain(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"^https?://", "", text)
    text = text.split("/")[0]
    if text.startswith("www."):
        text = text[4:]
    return text


def _sorted(sites: list[Website]) -> list[Website]:
    return sorted(sites, key=lambda site: (site.name.lower(), site.domain.lower(), site.id))


def _label(site: Website) -> str:
    if site.domain and site.name and site.name != site.id:
        return f"{site.name} ({site.domain})"
    if site.domain:
        return site.domain
    return site.name or site.id
