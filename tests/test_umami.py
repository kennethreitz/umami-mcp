import httpx
import pytest
import respx

from umami_mcp.client import UmamiClient, UmamiError
from umami_mcp.config import ConfigError, load_settings
from umami_mcp.service import Analytics

SITE_ID = "b8d3a1f0-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
OTHER_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _settings(**env: str):
    return load_settings(env)


def _websites_payload(*rows: dict[str, str], count: int | None = None) -> dict[str, object]:
    data = list(rows) or [{"id": SITE_ID, "name": "Example", "domain": "example.com"}]
    return {"data": data, "count": len(data) if count is None else count, "page": 1, "pageSize": 100}


@pytest.mark.asyncio
async def test_cloud_sends_both_credential_headers() -> None:
    settings = _settings(UMAMI_API_KEY="cloud-key")
    with respx.mock() as mock:
        route = mock.get("https://api.umami.is/v1/websites").mock(
            return_value=httpx.Response(200, json=_websites_payload())
        )
        async with UmamiClient(settings) as client:
            sites = await client.list_websites()
    assert [(site.name, site.domain) for site in sites] == [("Example", "example.com")]
    headers = route.calls[0].request.headers
    assert headers["x-umami-api-key"] == "cloud-key"
    assert headers["authorization"] == "Bearer cloud-key"
    assert route.calls[0].request.url.params["includeTeams"] == "true"
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_cloud_region_and_bearer_only_header() -> None:
    settings = _settings(UMAMI_API_KEY="cloud-key", UMAMI_CLOUD_REGION="eu", UMAMI_API_KEY_HEADER="bearer")
    assert settings.base_url == "https://api.umami.is/v1/eu"
    with respx.mock() as mock:
        route = mock.get("https://api.umami.is/v1/eu/websites").mock(
            return_value=httpx.Response(200, json=_websites_payload())
        )
        async with UmamiClient(settings) as client:
            await client.list_websites()
    headers = route.calls[0].request.headers
    assert headers["authorization"] == "Bearer cloud-key"
    assert "x-umami-api-key" not in headers


@pytest.mark.asyncio
async def test_self_hosted_login_and_refresh_on_401() -> None:
    settings = _settings(
        UMAMI_URL="https://analytics.example.com/",
        UMAMI_USERNAME="admin",
        UMAMI_PASSWORD="s3cret",
    )
    assert settings.api_prefix == "/api"
    tokens = iter(["first-token", "second-token"])
    with respx.mock() as mock:
        login = mock.post("https://analytics.example.com/api/auth/login").mock(
            side_effect=lambda request: httpx.Response(200, json={"token": next(tokens), "user": {}})
        )
        sites = mock.get("https://analytics.example.com/api/websites").mock(
            side_effect=[
                httpx.Response(401, json={"error": {"code": "unauthorized", "message": "Unauthorized.", "status": 401}}),
                httpx.Response(200, json=_websites_payload()),
            ]
        )
        async with UmamiClient(settings) as client:
            found = await client.list_websites()
    assert found[0].id == SITE_ID
    assert login.call_count == 2
    assert sites.call_count == 2
    assert sites.calls[0].request.headers["authorization"] == "Bearer first-token"
    assert sites.calls[1].request.headers["authorization"] == "Bearer second-token"
    body = login.calls[0].request.read()
    assert b"s3cret" in body
    assert "authorization" not in login.calls[0].request.headers


@pytest.mark.asyncio
async def test_api_key_401_does_not_try_a_password_login() -> None:
    settings = _settings(UMAMI_URL="https://analytics.example.com", UMAMI_API_KEY="site-key", UMAMI_USERNAME="admin")
    with respx.mock(assert_all_called=False) as mock:
        login = mock.post("https://analytics.example.com/api/auth/login").mock(return_value=httpx.Response(500))
        mock.get("https://analytics.example.com/api/websites").mock(
            return_value=httpx.Response(401, json={"error": {"message": "Unauthorized.", "status": 401}})
        )
        async with UmamiClient(settings) as client:
            with pytest.raises(UmamiError, match="401") as caught:
                await client.list_websites()
    assert caught.value.status == 401
    assert login.called is False


@pytest.mark.asyncio
async def test_two_factor_login_is_reported() -> None:
    settings = _settings(UMAMI_URL="https://analytics.example.com", UMAMI_USERNAME="admin", UMAMI_PASSWORD="s3cret")
    with respx.mock() as mock:
        mock.post("https://analytics.example.com/api/auth/login").mock(
            return_value=httpx.Response(200, json={"requiresTwoFactor": True, "partialToken": "partial"})
        )
        async with UmamiClient(settings) as client:
            with pytest.raises(UmamiError, match="two-factor"):
                await client.list_websites()


@pytest.mark.asyncio
async def test_stats_previous_period_both_response_shapes() -> None:
    settings = _settings(UMAMI_API_KEY="cloud-key", UMAMI_TIMEZONE="America/New_York")
    current = {
        "pageviews": "15171",
        "visitors": 4415,
        "visits": 5680,
        "bounces": None,
        "totaltime": "809968",
        "comparison": {
            "pageviews": 13000,
            "visitors": 4000,
            "visits": 5000,
            "bounces": 0,
            "totaltime": 400000,
        },
    }
    classic = {
        "pageviews": {"value": 10, "prev": 8},
        "visitors": {"value": 4, "prev": 4},
        "visits": {"value": 5, "prev": 10},
        "bounces": {"value": 1, "prev": 0},
        "totaltime": {"value": 120, "prev": 60},
    }
    with respx.mock() as mock:
        mock.get("https://api.umami.is/v1/websites").mock(return_value=httpx.Response(200, json=_websites_payload()))
        stats = mock.get(f"https://api.umami.is/v1/websites/{SITE_ID}/stats").mock(
            side_effect=[httpx.Response(200, json=current), httpx.Response(200, json=classic)]
        )
        async with UmamiClient(settings) as client:
            analytics = Analytics(client)
            text = await analytics.stats("example.com", "7d")
            classic_text = await analytics.stats("Example", "today", timezone="UTC")
    assert "Example (example.com)" in text
    assert "pageviews" in text and "15,171" in text
    assert "+16.7% vs previous (13,000)" in text
    assert "bounces" in text and "0" in text
    assert "avg" in text
    request = stats.calls[0].request
    assert request.url.params["compare"] == "prev"
    assert request.url.params["timezone"] == "America/New_York"
    assert request.url.params["startAt"].isdigit()
    assert "+25.0% vs previous (8)" in classic_text
    assert "0.0% vs previous (4)" in classic_text
    assert "-50.0% vs previous (10)" in classic_text
    assert "previous was 0" in classic_text


@pytest.mark.asyncio
async def test_pageviews_metrics_active_and_events() -> None:
    settings = _settings(UMAMI_API_KEY="cloud-key")
    with respx.mock() as mock:
        mock.get("https://api.umami.is/v1/websites").mock(return_value=httpx.Response(200, json=_websites_payload()))
        pageviews = mock.get(f"https://api.umami.is/v1/websites/{SITE_ID}/pageviews").mock(
            return_value=httpx.Response(
                200,
                json={
                    "pageviews": [{"x": "2026-11-08", "y": 12}, {"x": "2026-11-09", "y": "4"}],
                    "sessions": [{"x": "2026-11-08", "y": 3}, {"x": "2026-11-09", "y": 1}],
                },
            )
        )
        metrics = mock.get(f"https://api.umami.is/v1/websites/{SITE_ID}/metrics").mock(
            side_effect=lambda request: (
                httpx.Response(400, json={"error": {"message": "Bad request.", "status": 400}})
                if request.url.params["type"] == "path"
                else httpx.Response(200, json=[{"x": "/", "y": 9}, {"x": "/docs", "y": 2}])
            )
        )
        mock.get(f"https://api.umami.is/v1/websites/{SITE_ID}/active").mock(
            return_value=httpx.Response(200, json={"visitors": 1})
        )
        events = mock.get(f"https://api.umami.is/v1/websites/{SITE_ID}/events").mock(
            return_value=httpx.Response(
                200,
                json={
                    "count": 2,
                    "page": 1,
                    "pageSize": 20,
                    "data": [
                        {
                            "createdAt": "2026-11-09T14:02:00Z",
                            "eventName": "signup",
                            "eventType": 2,
                            "urlPath": "/pricing",
                            "referrerDomain": "google.com",
                        }
                    ],
                },
            )
        )
        async with UmamiClient(settings) as client:
            analytics = Analytics(client)
            series = await analytics.pageviews("example.com", "24h", timezone="UTC")
            breakdown = await analytics.metrics("www.example.com", "url", "7d", limit=10)
            active = await analytics.active(SITE_ID)
            recent = await analytics.events("example.com", "24h", timezone="America/New_York", kind="event")
    assert "pageviews" in series and "12" in series and "sessions" in series
    assert pageviews.calls[0].request.url.params["unit"] == "hour"
    assert metrics.call_count == 2
    assert metrics.calls[0].request.url.params["type"] == "path"
    assert metrics.calls[1].request.url.params["type"] == "url"
    assert "/docs" in breakdown
    assert "1 visitor" in active
    assert "signup" in recent and "/pricing" in recent and "google.com" in recent
    assert events.calls[0].request.url.params["eventType"] == "2"


@pytest.mark.asyncio
async def test_website_matching_and_list() -> None:
    settings = _settings(UMAMI_API_KEY="cloud-key")
    rows = [
        {"id": SITE_ID, "name": "Example", "domain": "www.example.com"},
        {"id": OTHER_ID, "name": "Other", "domain": "other.test"},
    ]
    with respx.mock() as mock:
        mock.get("https://api.umami.is/v1/websites").mock(return_value=httpx.Response(200, json=_websites_payload(*rows)))
        async with UmamiClient(settings) as client:
            analytics = Analytics(client)
            listed = await analytics.list_websites("example")
            with pytest.raises(UmamiError, match=OTHER_ID):
                await analytics.stats("e", "7d")
            with pytest.raises(UmamiError, match="No website"):
                await analytics.active("missing.example")
    assert "Example" in listed
    assert "www.example.com" in listed
    assert "Other" not in listed


def test_configuration_rules() -> None:
    with pytest.raises(ConfigError, match="UMAMI_API_KEY"):
        load_settings({})
    with pytest.raises(ConfigError, match="UMAMI_URL"):
        load_settings({"UMAMI_USERNAME": "admin", "UMAMI_PASSWORD": "x"})
    with pytest.raises(ConfigError, match="Cloud"):
        load_settings({"UMAMI_URL": "https://api.umami.is/v1", "UMAMI_USERNAME": "a", "UMAMI_PASSWORD": "b"})
    with pytest.raises(ConfigError, match="MCP_AUTH_TOKEN"):
        load_settings({"UMAMI_API_KEY": "k", "MCP_TRANSPORT": "http"})
    settings = load_settings({"UMAMI_API_KEY": "k", "UMAMI_URL": "https://analytics.example.com/api"})
    assert settings.api_prefix == ""
    assert settings.base_url == "https://analytics.example.com/api"
