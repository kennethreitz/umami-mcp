"""Environment configuration for Umami and the MCP transport."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CLOUD_API_ROOT = "https://api.umami.is/v1"
DEFAULT_TIMEZONE = "America/New_York"
API_KEY_HEADER_MODES = ("both", "bearer", "x-umami-api-key")


class ConfigError(Exception):
    """The process was started with missing or invalid configuration."""


@dataclass(frozen=True)
class Settings:
    """Resolved settings. `base_url` never has a trailing slash.

    `api_prefix` is `/api` for a self-hosted site origin and empty when `base_url`
    already points at the API root (Umami Cloud, or a URL that ends in `/api` or `/v1`).
    """

    base_url: str
    api_prefix: str
    api_key: str | None
    username: str | None
    password: str | None
    api_key_header: str
    timezone: str
    transport: str
    host: str
    port: int
    mcp_auth_token: str | None
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]

    @property
    def uses_api_key(self) -> bool:
        return self.api_key is not None

    @property
    def cloud(self) -> bool:
        return _is_cloud(self.base_url)


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Read settings from `env`, or from the process environment when `env` is omitted."""

    source = os.environ if env is None else env
    api_key = _blank(source.get("UMAMI_API_KEY"))
    username = _blank(source.get("UMAMI_USERNAME"))
    password = _blank(source.get("UMAMI_PASSWORD"))
    url = _blank(source.get("UMAMI_URL"))
    region = (_blank(source.get("UMAMI_CLOUD_REGION")) or "").lower() or None

    if region not in {None, "us", "eu"}:
        raise ConfigError("UMAMI_CLOUD_REGION must be 'us' or 'eu'.")

    if not api_key and not (username and password):
        raise ConfigError(
            "Set UMAMI_API_KEY (Umami Cloud or a self-hosted API key), "
            "or both UMAMI_USERNAME and UMAMI_PASSWORD for a self-hosted login."
        )
    if not api_key and bool(username) != bool(password):
        raise ConfigError("Set both UMAMI_USERNAME and UMAMI_PASSWORD, or use UMAMI_API_KEY instead.")

    if api_key and not url:
        url = CLOUD_API_ROOT
    elif not url:
        raise ConfigError(
            "UMAMI_URL is required for username/password login. "
            "Use the site origin, for example https://analytics.example.com."
        )

    url = url.rstrip("/")
    if _is_cloud(url):
        if not api_key:
            raise ConfigError(
                "Umami Cloud does not accept username/password login. "
                "Create an API key and set UMAMI_API_KEY."
            )
        url = _apply_cloud_region(url, region)
    elif region:
        raise ConfigError("UMAMI_CLOUD_REGION is only used with Umami Cloud (https://api.umami.is).")

    header_mode = (_blank(source.get("UMAMI_API_KEY_HEADER")) or "both").lower()
    if header_mode not in API_KEY_HEADER_MODES:
        allowed = ", ".join(API_KEY_HEADER_MODES)
        raise ConfigError(f"UMAMI_API_KEY_HEADER must be one of: {allowed}.")

    timezone = _blank(source.get("UMAMI_TIMEZONE")) or DEFAULT_TIMEZONE
    _require_timezone(timezone)

    transport = (_blank(source.get("MCP_TRANSPORT")) or "stdio").lower().replace("_", "-")
    if transport == "streamable-http":
        transport = "http"
    if transport not in {"stdio", "http"}:
        raise ConfigError("MCP_TRANSPORT must be 'stdio' or 'http'.")

    host = _blank(source.get("MCP_HOST")) or ("0.0.0.0" if transport == "http" else "127.0.0.1")
    port = _port(source.get("PORT") or source.get("MCP_PORT") or "8787")
    token = _blank(source.get("MCP_AUTH_TOKEN"))
    if transport == "http" and not token:
        raise ConfigError("MCP_AUTH_TOKEN is required when MCP_TRANSPORT=http. Refusing to listen without it.")

    return Settings(
        base_url=url,
        api_prefix="" if _api_root(url) else "/api",
        api_key=api_key,
        username=username,
        password=password,
        api_key_header=header_mode,
        timezone=timezone,
        transport=transport,
        host=host,
        port=port,
        mcp_auth_token=token,
        allowed_hosts=_csv(source.get("MCP_ALLOWED_HOSTS")),
        allowed_origins=_csv(source.get("MCP_ALLOWED_ORIGINS")),
    )


def _blank(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _port(value: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"PORT must be an integer, got {value!r}.") from exc
    if not 1 <= port <= 65535:
        raise ConfigError(f"PORT must be between 1 and 65535, got {port}.")
    return port


def _require_timezone(name: str) -> None:
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"Unknown timezone {name!r}. Use an IANA name such as America/New_York.") from exc


def _is_cloud(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() == "api.umami.is"


def _apply_cloud_region(url: str, region: str | None) -> str:
    if not region:
        return url
    path = urlparse(url).path.rstrip("/")
    if path.endswith("/us") or path.endswith("/eu"):
        return url
    return f"{url}/{region}"


def _api_root(url: str) -> bool:
    """True when paths are appended directly (`/websites`), not under `/api`."""

    if _is_cloud(url):
        return True
    path = urlparse(url).path.rstrip("/").lower()
    return path.endswith("/api") or path.endswith("/v1") or path.endswith("/v1/us") or path.endswith("/v1/eu")
