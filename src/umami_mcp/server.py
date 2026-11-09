"""MCP server. Tools are read-only and return short text."""

from __future__ import annotations

import hmac
from collections.abc import Callable

from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from umami_mcp.client import UmamiError
from umami_mcp.config import ConfigError
from umami_mcp.service import Analytics

INSTRUCTIONS = """\
Read-only access to a Umami analytics account. Use list_websites when the site \
name or domain is unclear, then pass that id, name, or domain to the other tools.

Date ranges: today, yesterday, 24h, 7d, 30d, 90d, this week, last week, this month, \
last month, this year, last year, or ISO dates (2026-01-01 or 2026-01-01/2026-01-31). \
Times use the timezone argument. When it is omitted, the server default is used \
(America/New_York unless UMAMI_TIMEZONE is set).

get_stats includes pageviews, visitors, visits, bounces, and total time, each \
compared with the previous period of the same length. get_metrics ranks one \
dimension (path, referrer, browser, os, device, country, event, and others). \
Nothing here creates, edits, or deletes analytics data.
"""

_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)

class StaticBearerVerifier(TokenVerifier):
    """Accept one pre-shared bearer token. Comparison is constant-time for equal lengths."""

    def __init__(self, expected: str) -> None:
        self._expected = expected.encode("utf-8")

    async def verify_token(self, token: str) -> AccessToken | None:
        presented = token.encode("utf-8")
        if len(presented) != len(self._expected) or not hmac.compare_digest(presented, self._expected):
            return None
        return AccessToken(token=token, client_id="umami-mcp", scopes=["read"])


def create_server(analytics: Analytics, *, auth_token: str | None = None) -> MCPServer:
    """Build the MCP server. HTTP mode passes `auth_token` from MCP_AUTH_TOKEN."""

    kwargs: dict[str, object] = {}
    if auth_token:
        # Static bearer check, not an OAuth login. issuer_url is required by the SDK
        # and is not a real authorization server. resource_server_url is omitted so
        # clients are not sent through OAuth discovery; they send the bearer token.
        kwargs["token_verifier"] = StaticBearerVerifier(auth_token)
        kwargs["auth"] = AuthSettings(
            issuer_url=AnyHttpUrl("https://umami-mcp.local"),
            resource_server_url=None,
            required_scopes=["read"],
            validate_token_resource=False,
        )

    mcp = MCPServer("Umami", instructions=INSTRUCTIONS, **kwargs)

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/", methods=["GET"])
    async def root(_: Request) -> Response:
        return PlainTextResponse(
            "Umami MCP server\nConnect a client to /mcp with Authorization: Bearer <MCP_AUTH_TOKEN>.\n"
        )

    def tool(fn: Callable[..., object]) -> Callable[..., object]:
        return mcp.tool(structured_output=False, annotations=_READ_ONLY)(fn)

    @tool
    async def list_websites(query: str | None = None) -> str:
        """List websites this account can read.

        query matches a name or domain (exact match first, then a substring).
        Team websites are included. Pass an id from this list to the other tools
        when more than one site could match.
        """

        return await _call(analytics.list_websites(query))

    @tool
    async def get_stats(
        website: str,
        date_range: str = "7d",
        timezone: str | None = None,
        path: str | None = None,
        referrer: str | None = None,
        browser: str | None = None,
        os: str | None = None,
        device: str | None = None,
        country: str | None = None,
        event: str | None = None,
    ) -> str:
        """Pageviews, visitors, visits, bounces, and total time for a date range.

        Each number is compared with the previous period of the same length.
        website is an id, name, or domain. date_range accepts today, 7d, 30d,
        this month, or ISO dates. timezone is an IANA name; the default is
        America/New_York unless UMAMI_TIMEZONE is set.
        """

        return await _call(
            analytics.stats(
                website,
                date_range,
                timezone,
                path=path,
                referrer=referrer,
                browser=browser,
                os=os,
                device=device,
                country=country,
                event=event,
            )
        )

    @tool
    async def get_pageviews(
        website: str,
        date_range: str = "7d",
        timezone: str | None = None,
        unit: str | None = None,
        path: str | None = None,
        referrer: str | None = None,
        browser: str | None = None,
        os: str | None = None,
        device: str | None = None,
        country: str | None = None,
        event: str | None = None,
    ) -> str:
        """Pageviews and sessions over time.

        unit is minute, hour, day, month, or year. When omitted, a short range
        uses hours and a longer one uses days or months. See get_stats for
        website, date_range, and timezone.
        """

        return await _call(
            analytics.pageviews(
                website,
                date_range,
                timezone,
                unit,
                path=path,
                referrer=referrer,
                browser=browser,
                os=os,
                device=device,
                country=country,
                event=event,
            )
        )

    @tool
    async def get_metrics(
        website: str,
        metric: str = "path",
        date_range: str = "7d",
        timezone: str | None = None,
        limit: int = 15,
        path: str | None = None,
        referrer: str | None = None,
        browser: str | None = None,
        os: str | None = None,
        device: str | None = None,
        country: str | None = None,
        event: str | None = None,
    ) -> str:
        """Rank one dimension: path, referrer, browser, os, device, country, event, and others.

        metric also accepts entry, exit, title, query, hostname, domain, channel,
        tag, screen, language, region, city, and UTM fields. url is an alias for
        path (older Umami servers called this dimension url). limit is 1 to 100.
        """

        return await _call(
            analytics.metrics(
                website,
                metric,
                date_range,
                timezone,
                limit,
                path=path,
                referrer=referrer,
                browser=browser,
                os=os,
                device=device,
                country=country,
                event=event,
            )
        )

    @tool
    async def get_active_visitors(website: str) -> str:
        """How many visitors are on the website right now.

        This is Umami's active-visitor count (the last few minutes).
        """

        return await _call(analytics.active(website))

    @tool
    async def get_events(
        website: str,
        date_range: str = "7d",
        timezone: str | None = None,
        event: str | None = None,
        search: str | None = None,
        limit: int = 20,
        kind: str = "event",
    ) -> str:
        """Recent tracked events, newest first.

        kind is event (custom events, the default), pageview, or all.
        event filters by custom event name. search matches event details.
        limit is 1 to 100 and is one page, not every event in the range.
        """

        return await _call(
            analytics.events(
                website,
                date_range,
                timezone,
                event=event,
                search=search,
                limit=limit,
                kind=kind,
            )
        )

    return mcp


async def _call(awaitable: object) -> str:
    try:
        result = await awaitable  # type: ignore[misc]
    except (UmamiError, ConfigError) as exc:
        raise ToolError(str(exc)) from exc
    if not isinstance(result, str):
        raise ToolError("Tool returned an unexpected result.")
    return result
