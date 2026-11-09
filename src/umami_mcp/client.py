"""HTTP client for Umami Cloud and self-hosted Umami.

Cloud (and self-hosted API keys) send the key as `x-umami-api-key` and as
`Authorization: Bearer`. Current Umami docs describe the bearer header; older
Cloud clients and the Cloud MCP docs also describe `x-umami-api-key`. Sending
both matches either gateway. `UMAMI_API_KEY_HEADER` can force one of them.

Self-hosted username/password login posts to `/api/auth/login` and retries once
after a 401 with a fresh token. API keys are not refreshed.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from umami_mcp import __version__
from umami_mcp.config import Settings
from umami_mcp.dates import DateWindow
from umami_mcp.formatting import as_int

logger = logging.getLogger(__name__)

USER_AGENT = f"umami-mcp/{__version__}"
_MAX_WEBSITE_PAGES = 20


class UmamiError(Exception):
    """A call to Umami failed in a way the caller can explain."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Website:
    id: str
    name: str
    domain: str


class UmamiClient:
    """Small async wrapper around the read endpoints this server exposes."""

    def __init__(self, settings: Settings, *, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._owns_http = http is None
        self._token: str | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> UmamiClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def list_websites(self) -> list[Website]:
        page = 1
        page_size = 100
        found: list[Website] = []
        seen: set[str] = set()
        while page <= _MAX_WEBSITE_PAGES:
            payload = await self._get(
                "/websites",
                params={"page": page, "pageSize": page_size, "includeTeams": "true"},
            )
            rows, count = _page_rows(payload)
            if not rows:
                break
            fresh = False
            for row in rows:
                website = _website(row)
                if website is None or website.id in seen:
                    continue
                seen.add(website.id)
                found.append(website)
                fresh = True
            if not fresh or len(rows) < page_size or (count is not None and len(found) >= count):
                break
            page += 1
        return found

    async def stats(
        self,
        website_id: str,
        window: DateWindow,
        *,
        timezone: str,
        filters: dict[str, str],
    ) -> dict[str, Any]:
        payload = await self._get(
            f"/websites/{_quote_id(website_id)}/stats",
            params=_range_params(window, timezone, filters, compare=True),
        )
        if not isinstance(payload, dict):
            raise UmamiError("Umami stats response was not an object.")
        return payload

    async def pageviews(
        self,
        website_id: str,
        window: DateWindow,
        *,
        timezone: str,
        unit: str,
        filters: dict[str, str],
    ) -> dict[str, Any]:
        params = _range_params(window, timezone, filters, compare=True)
        params["unit"] = unit
        payload = await self._get(f"/websites/{_quote_id(website_id)}/pageviews", params=params)
        if not isinstance(payload, dict):
            raise UmamiError("Umami pageviews response was not an object.")
        return payload

    async def metrics(
        self,
        website_id: str,
        window: DateWindow,
        *,
        timezone: str,
        metric: str,
        limit: int,
        filters: dict[str, str],
    ) -> Any:
        try:
            return await self._metrics(website_id, window, timezone, metric, limit, filters)
        except UmamiError as exc:
            # Current docs use type=path. Older v2 docs used type=url.
            if metric == "path" and exc.status == 400:
                logger.info("metrics type=path was rejected; retrying type=url")
                return await self._metrics(website_id, window, timezone, "url", limit, filters)
            raise

    async def active(self, website_id: str) -> int:
        payload = await self._get(f"/websites/{_quote_id(website_id)}/active")
        if isinstance(payload, int):
            return payload
        if isinstance(payload, dict):
            for key in ("visitors", "x", "value"):
                if key in payload:
                    return as_int(payload.get(key)) or 0
        raise UmamiError("Umami active-visitors response was not a count.")

    async def events(
        self,
        website_id: str,
        window: DateWindow,
        *,
        timezone: str,
        limit: int,
        event: str | None,
        search: str | None,
        event_type: int | None,
    ) -> Any:
        params = _range_params(window, timezone, {}, compare=False)
        params["page"] = 1
        params["pageSize"] = limit
        if event:
            params["event"] = event
        if search:
            params["search"] = search
        if event_type is not None:
            params["eventType"] = event_type
        return await self._get(f"/websites/{_quote_id(website_id)}/events", params=params)

    async def _metrics(
        self,
        website_id: str,
        window: DateWindow,
        timezone: str,
        metric: str,
        limit: int,
        filters: dict[str, str],
    ) -> Any:
        params = _range_params(window, timezone, filters, compare=False)
        params["type"] = metric
        params["limit"] = limit
        return await self._get(f"/websites/{_quote_id(website_id)}/metrics", params=params)

    async def _get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None) -> Any:
        response: httpx.Response | None = None
        for attempt in (1, 2):
            if not self.settings.uses_api_key:
                await self._ensure_token()
            response = await self._send(method, path, params=params)
            if response.status_code == 401 and not self.settings.uses_api_key and attempt == 1:
                logger.info("Umami returned 401; signing in again")
                async with self._lock:
                    self._token = None
                    await self._login()
                continue
            break
        assert response is not None
        return _decode(response)

    async def _ensure_token(self) -> None:
        if self._token:
            return
        async with self._lock:
            if not self._token:
                await self._login()

    async def _login(self) -> None:
        url = self._url("/auth/login")
        logger.info("POST %s", url)
        try:
            response = await self._http.post(
                url,
                json={"username": self.settings.username, "password": self.settings.password},
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )
        except httpx.HTTPError as exc:
            raise UmamiError(f"Could not reach Umami ({exc.__class__.__name__}).") from exc
        if response.status_code >= 400:
            raise UmamiError(_error_text(response), status=response.status_code)
        try:
            body = response.json()
        except ValueError as exc:
            raise UmamiError("Umami login did not return JSON.") from exc
        if not isinstance(body, dict):
            raise UmamiError("Umami login did not return a token.")
        if body.get("requiresTwoFactor"):
            raise UmamiError(
                "This Umami account has two-factor authentication turned on. "
                "Create an API key under Settings → API keys and set UMAMI_API_KEY."
            )
        token = body.get("token")
        if not isinstance(token, str) or not token:
            raise UmamiError("Umami login did not return a token.")
        self._token = token

    async def _send(self, method: str, path: str, *, params: dict[str, Any] | None) -> httpx.Response:
        url = self._url(path)
        logger.debug("%s %s", method, url)
        try:
            return await self._http.request(method, url, params=params, headers=self._headers())
        except httpx.HTTPError as exc:
            raise UmamiError(f"Could not reach Umami ({exc.__class__.__name__}).") from exc

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if self.settings.api_key:
            mode = self.settings.api_key_header
            if mode in {"both", "x-umami-api-key"}:
                headers["x-umami-api-key"] = self.settings.api_key
            if mode in {"both", "bearer"}:
                headers["Authorization"] = f"Bearer {self.settings.api_key}"
            return headers
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _url(self, path: str) -> str:
        suffix = path if path.startswith("/") else f"/{path}"
        return f"{self.settings.base_url}{self.settings.api_prefix}{suffix}"


def _range_params(
    window: DateWindow,
    timezone: str,
    filters: dict[str, str],
    *,
    compare: bool,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "startAt": window.start_ms,
        "endAt": window.end_ms,
        "timezone": timezone,
    }
    if compare:
        params["compare"] = "prev"
    params.update(filters)
    return params


def _decode(response: httpx.Response) -> Any:
    if response.status_code == 401:
        raise UmamiError(
            "Umami rejected the credentials (401). Check UMAMI_API_KEY, or the username and password.",
            status=401,
        )
    if response.status_code == 429:
        raise UmamiError(
            "Umami rate limit reached. Cloud API keys allow 50 calls every 15 seconds.",
            status=429,
        )
    if response.status_code >= 400:
        raise UmamiError(_error_text(response), status=response.status_code)
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise UmamiError(f"Umami returned {response.status_code} with a body that was not JSON.") from exc


def _error_text(response: httpx.Response) -> str:
    message = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            message = str(error["message"])
        elif isinstance(body.get("message"), str):
            message = body["message"]
    if not message:
        message = response.text.strip()[:300] or "request failed"
    return f"Umami returned {response.status_code}: {message}"


def _page_rows(payload: Any) -> tuple[list[Any], int | None]:
    if isinstance(payload, list):
        return payload, len(payload)
    if isinstance(payload, dict):
        data = payload.get("data")
        rows = data if isinstance(data, list) else []
        return rows, as_int(payload.get("count"))
    raise UmamiError("Umami websites response was not a list.")


def _website(row: Any) -> Website | None:
    if not isinstance(row, dict):
        return None
    website_id = row.get("id") or row.get("websiteId")
    if not isinstance(website_id, str) or not website_id:
        return None
    name = row.get("name") if isinstance(row.get("name"), str) else ""
    domain = row.get("domain") if isinstance(row.get("domain"), str) else ""
    return Website(id=website_id, name=name, domain=domain)


def _quote_id(website_id: str) -> str:
    return quote(website_id, safe="")
