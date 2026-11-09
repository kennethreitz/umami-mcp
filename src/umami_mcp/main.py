"""Process entrypoint. stdio by default; HTTP when MCP_TRANSPORT=http."""

from __future__ import annotations

import logging
import sys
from dataclasses import replace

from mcp.server.transport_security import TransportSecuritySettings

from umami_mcp.client import UmamiClient
from umami_mcp.config import ConfigError, Settings, load_settings
from umami_mcp.server import create_server
from umami_mcp.service import Analytics


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = list(sys.argv[1:] if argv is None else argv)
    override: str | None = None
    if args:
        if len(args) == 1 and args[0] in {"stdio", "http"}:
            override = args[0]
        else:
            raise SystemExit("usage: umami-mcp [stdio|http]")

    try:
        settings = load_settings()
    except ConfigError as exc:
        raise SystemExit(f"umami-mcp: {exc}") from exc

    if override:
        settings = replace(settings, transport=override)
    if settings.transport == "http" and not settings.mcp_auth_token:
        raise SystemExit("umami-mcp: MCP_AUTH_TOKEN is required when MCP_TRANSPORT=http. Refusing to listen without it.")

    analytics = Analytics(UmamiClient(settings))
    token = settings.mcp_auth_token if settings.transport == "http" else None
    server = create_server(analytics, auth_token=token)
    if settings.transport == "http":
        _run_http(server, settings)
        return
    server.run(transport="stdio")


def _run_http(server: object, settings: Settings) -> None:
    security = None
    if settings.allowed_hosts or settings.allowed_origins:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(settings.allowed_hosts),
            allowed_origins=list(settings.allowed_origins),
        )
    logging.getLogger(__name__).info(
        "Umami MCP listening on http://%s:%s/mcp",
        settings.host,
        settings.port,
    )
    server.run(  # type: ignore[attr-defined]
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        json_response=True,
        stateless_http=True,
        transport_security=security,
    )


if __name__ == "__main__":
    main()
