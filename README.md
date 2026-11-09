# Umami MCP

A read-only [MCP](https://modelcontextprotocol.io) server for [Umami](https://umami.is). An assistant can list your websites and read stats, pageviews, breakdowns, active visitors, and events.

It speaks **stdio** for a local client and **streamable HTTP** for a hosted one. HTTP mode refuses to start unless `MCP_AUTH_TOKEN` is set.

The server uses the official `mcp` Python SDK. In SDK 2.x the high-level class is `MCPServer` (the class that used to be called FastMCP).

## Tools

| Tool | What it returns |
| --- | --- |
| `list_websites` | Websites you can read, including team sites. Optional `query` matches name or domain. |
| `get_stats` | Pageviews, visitors, visits, bounces, and total time, each compared with the previous period of the same length. |
| `get_pageviews` | Pageviews and sessions over time. The bucket is chosen from the range unless you pass `unit`. |
| `get_metrics` | A ranked breakdown. `metric` is `path`, `referrer`, `browser`, `os`, `device`, `country`, `event`, and the other dimensions listed in the tool description. |
| `get_active_visitors` | Visitors active in the last few minutes. |
| `get_events` | Recent custom events, newest first. `kind` can be `event`, `pageview`, or `all`. |

Pass a website **id, name, or domain**. Exact name or domain wins; otherwise a unique substring match is used. If several sites match, the tool lists them and stops.

Date ranges accept `today`, `yesterday`, `24h`, `7d`, `30d`, `90d`, `this week`, `last week`, `this month`, `last month`, `this year`, `last year`, a day (`2026-01-15`), or a span (`2026-01-01/2026-01-31` or `2026-01-01 to 2026-01-31`). Ranges are resolved in `timezone` (IANA name). The default is `America/New_York`, or `UMAMI_TIMEZONE` when that is set.

Every tool is marked read-only. There is no write endpoint.

## Configure

Copy `.env.example` to `.env` if you want a file to edit. The process reads the environment; it does not load `.env` by itself.

| Variable | Required | Purpose |
| --- | --- | --- |
| `UMAMI_API_KEY` | One of these auth modes | Cloud API key, or a self-hosted API key. |
| `UMAMI_URL` | Self-hosted | Site origin, such as `https://analytics.example.com`. Not required for Cloud when `UMAMI_API_KEY` is set. |
| `UMAMI_USERNAME` / `UMAMI_PASSWORD` | Self-hosted login | Used together when you do not have an API key. A 401 fetches a new token once. |
| `UMAMI_CLOUD_REGION` | No | `us` or `eu`. Sends Cloud calls to `https://api.umami.is/v1/us` or `/eu`. |
| `UMAMI_API_KEY_HEADER` | No | `both` (default), `bearer`, or `x-umami-api-key`. See [API notes](#api-notes). |
| `UMAMI_TIMEZONE` | No | Default timezone. `America/New_York` when unset. |
| `MCP_TRANSPORT` | No | `stdio` (default) or `http`. |
| `MCP_AUTH_TOKEN` | HTTP only | Bearer token clients must send. The server will not listen without it. |
| `MCP_HOST` | No | Bind address. `0.0.0.0` for HTTP, `127.0.0.1` for stdio's unused default. |
| `PORT` or `MCP_PORT` | No | HTTP port. Default `8787`. `PORT` wins when both are set. |
| `MCP_ALLOWED_HOSTS` | No | Comma-separated `Host` values. Turns on DNS-rebinding checks. |
| `MCP_ALLOWED_ORIGINS` | No | Comma-separated `Origin` values. Used with the host list. |

Auth rules:

- **Umami Cloud:** set `UMAMI_API_KEY`. Leave `UMAMI_URL` empty unless you need a specific base. Cloud does not accept username and password.
- **Self-hosted API key:** set `UMAMI_URL` and `UMAMI_API_KEY`.
- **Self-hosted login:** set `UMAMI_URL`, `UMAMI_USERNAME`, and `UMAMI_PASSWORD`. If the account has two-factor authentication, create an API key instead. This server does not complete the second factor.
- If both an API key and a password are set, the API key is used.

`UMAMI_URL` for a self-hosted site is the origin (`https://analytics.example.com`). Requests go to `{origin}/api/...`. If the URL already ends in `/api` or `/v1`, that suffix is kept and `/api` is not added again. Cloud calls go to `https://api.umami.is/v1/...` with no extra `/api`.

## Run locally with uv

Install [uv](https://docs.astral.sh/uv/), then:

```bash
uv sync
export UMAMI_API_KEY="your-cloud-key"
uv run umami-mcp
```

That is stdio. The process waits on stdin; a client has to speak first. Nothing is printed on stdout, because stdout is the MCP stream.

HTTP, for a client that connects by URL:

```bash
export UMAMI_API_KEY="your-cloud-key"
export MCP_TRANSPORT=http
export MCP_AUTH_TOKEN="$(openssl rand -hex 32)"
uv run umami-mcp
```

Or `uv run umami-mcp http`. The MCP endpoint is `http://127.0.0.1:8787/mcp`. `GET /health` is open so a process supervisor can probe it. `GET /` tells a browser where to connect. Everything under `/mcp` requires `Authorization: Bearer $MCP_AUTH_TOKEN`.

Self-hosted example:

```bash
export UMAMI_URL="https://analytics.example.com"
export UMAMI_USERNAME="admin"
export UMAMI_PASSWORD="..."
uv run umami-mcp
```

## Connect a client

### Hosted (streamable HTTP)

Cursor, Claude, and other clients that accept a remote MCP URL:

```json
{
  "mcpServers": {
    "umami": {
      "url": "https://umami-mcp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_AUTH_TOKEN"
      }
    }
  }
}
```

Put the server's public HTTPS URL in `url`. The token is `MCP_AUTH_TOKEN`, not the Umami API key. The Umami key stays on the server.

### Local stdio

```json
{
  "mcpServers": {
    "umami": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/this/repo", "umami-mcp"],
      "env": {
        "UMAMI_API_KEY": "your-cloud-key"
      }
    }
  }
}
```

For a self-hosted Umami, set `UMAMI_URL` plus either `UMAMI_API_KEY` or `UMAMI_USERNAME` and `UMAMI_PASSWORD` in `env`.

## Deploy

The container listens on `0.0.0.0` and `$PORT` (default `8787`) with `MCP_TRANSPORT=http`. Build it from this directory:

```bash
docker build -t umami-mcp .
docker run --rm -p 8787:8787 \
  -e UMAMI_API_KEY \
  -e MCP_AUTH_TOKEN \
  umami-mcp
```

Pass Umami credentials and `MCP_AUTH_TOKEN` at run time. Do not bake them into the image. `/health` returns `{"status":"ok"}` without a token.

HTTP mode is stateless and answers each MCP POST with a single JSON body, so more than one replica does not need sticky sessions.

### Dokploy

1. Create an application from this repo (Dockerfile build) or from the image.
2. Set the container port to `8787`, or set `PORT` to the port Dokploy routes to and use that.
3. Add environment variables: `UMAMI_API_KEY` (or `UMAMI_URL` plus username and password), and `MCP_AUTH_TOKEN`.
4. Point the health check at `GET /health`.
5. Attach a domain and HTTPS. Give clients `https://<that-domain>/mcp` and the bearer token.

### Cloudflare (container, not a Worker)

This is a long-running Python process. Run it as a [Cloudflare Container](https://developers.cloudflare.com/containers/), or any other host that runs a Docker image. A Worker cannot host this process. Route public HTTPS to the container port (`PORT`, default `8787`). Keep `MCP_AUTH_TOKEN` as the application secret; Cloudflare Access can sit in front as a second gate, but the MCP client still has to send the bearer token unless you strip that check yourself.

Leave `MCP_ALLOWED_HOSTS` empty when a proxy rewrites `Host` to your public name. Setting it turns on DNS-rebinding checks, and the proxy's `Host` has to be on the list. Binding `MCP_HOST=127.0.0.1` also turns those checks on for localhost only, which is what you want on a laptop and the wrong choice inside a container.

## Development

```bash
uv sync --group dev
uv run pytest
```

Tests mock Umami with `respx`. One test starts the real server over stdio and checks `tools/list`. Another starts HTTP, checks that `/health` is open, that `/mcp` returns 401 without the bearer token, and that a client with the token can list the tools.

## API notes

Checked against the Umami docs on docs.umami.is (the current reference) and the older v2 reference on v2.umami.is. No live Cloud or self-hosted instance was called from this repo, so a few shapes are handled defensively rather than confirmed against production.

**Auth headers.** The current Cloud and self-hosted docs say to send an API key as `Authorization: Bearer <key>`. Older Cloud reports, and Umami's own Cloud MCP page, also describe `x-umami-api-key`. The default sends **both** the same key. Set `UMAMI_API_KEY_HEADER=bearer` or `UMAMI_API_KEY_HEADER=x-umami-api-key` to send only one. Which header a given Cloud gateway requires was not verified with a live key.

**Paths.** Self-hosted: `POST {UMAMI_URL}/api/auth/login`, then `GET {UMAMI_URL}/api/websites`, `GET /api/websites/{id}/stats`, `/pageviews`, `/metrics`, `/active`, `/events`. Cloud: the same paths under `https://api.umami.is/v1` with no `/api` prefix. Optional region prefix: `/v1/us` or `/v1/eu`.

**Stats comparison.** Current docs return flat numbers plus a `comparison` object, and accept `compare=prev` (or `yoy`). Older v2 responses nest `{ "value", "prev" }` on each stat and include the previous period without that parameter. This server sends `compare=prev` and understands both bodies. `compare=yoy` is not requested. The previous window printed in the text is an equal-length range immediately before the one you asked for, which matches the documented comparison. The exact millisecond boundary on a live server was not rechecked.

**Numbers.** Stats sometimes arrive as strings or null (reported against the current API). Those are coerced. `totaltime` is treated as seconds. The average shown is total time divided by visits. Umami's dashboard has used other formulas (for example excluding bounces). Treat the average as a reading of the API field, not a pixel-perfect copy of the dashboard card.

**Metrics.** Current docs use `type=path`. Older v2 docs use `type=url`. A request for pages sends `path`, and on HTTP 400 retries once with `url`.

**Active visitors.** The common-endpoints page says the count covers the last 5 minutes. The active-endpoint page says "the last few minutes." The tool uses the latter wording and does not invent a window the payload does not include.

**Events.** `eventType` 1 is a pageview and 2 is a custom event, from the current reference. The default asks for custom events only.

**Rate limit.** Cloud docs say 50 calls per API key every 15 seconds. A 429 is returned as a tool error. This was not measured.

**Login.** `POST /api/auth/login` with `{ "username", "password" }` returns `{ "token", "user" }`. A body with `requiresTwoFactor` and no token stops with a message to use an API key. The `/api/2fa/verify` step is not implemented.
