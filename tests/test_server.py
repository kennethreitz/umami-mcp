"""The server process speaks MCP and, over HTTP, rejects anonymous clients."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import httpx2
import pytest
import respx
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from umami_mcp.client import UmamiClient
from umami_mcp.config import load_settings
from umami_mcp.server import create_server
from umami_mcp.service import Analytics

SITE_ID = "b8d3a1f0-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = [
    "list_websites",
    "get_stats",
    "get_pageviews",
    "get_metrics",
    "get_active_visitors",
    "get_events",
]


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _tool_text(result: object) -> str:
    content = getattr(result, "content", None)
    assert content, result
    block = content[0]
    text = getattr(block, "text", None)
    assert isinstance(text, str)
    return text


@pytest.mark.asyncio
async def test_server_lists_read_only_tools() -> None:
    settings = load_settings({"UMAMI_API_KEY": "test-key"})
    server = create_server(Analytics(UmamiClient(settings)))
    async with Client(server) as client:
        listed = await client.list_tools()
    names = [tool.name for tool in listed.tools]
    assert names == EXPECTED_TOOLS
    assert all(tool.annotations is not None and tool.annotations.read_only_hint for tool in listed.tools)
    stats = next(tool for tool in listed.tools if tool.name == "get_stats")
    assert "previous period" in (stats.description or "").lower()


@pytest.mark.asyncio
async def test_tool_call_returns_text_and_errors() -> None:
    settings = load_settings({"UMAMI_API_KEY": "test-key", "UMAMI_TIMEZONE": "UTC"})
    payload = {
        "data": [
            {"id": SITE_ID, "name": "Example", "domain": "example.com"},
            {"id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "name": "Example Docs", "domain": "docs.example.com"},
        ],
        "count": 2,
        "page": 1,
        "pageSize": 100,
    }
    with respx.mock() as mock:
        mock.get("https://api.umami.is/v1/websites").mock(return_value=httpx.Response(200, json=payload))
        mock.get(f"https://api.umami.is/v1/websites/{SITE_ID}/stats").mock(
            return_value=httpx.Response(
                200,
                json={
                    "pageviews": 10,
                    "visitors": 4,
                    "visits": 5,
                    "bounces": 1,
                    "totaltime": 90,
                    "comparison": {
                        "pageviews": 5,
                        "visitors": 2,
                        "visits": 3,
                        "bounces": 1,
                        "totaltime": 30,
                    },
                },
            )
        )
        server = create_server(Analytics(UmamiClient(settings)))
        async with Client(server) as client:
            stats = await client.call_tool(
                "get_stats",
                {"website": "example.com", "date_range": "7d", "timezone": "UTC"},
            )
            ambiguous = await client.call_tool("get_active_visitors", {"website": "ex"})
            broken = await client.call_tool("get_pageviews", {"website": "example.com", "date_range": "nope"})
    assert stats.is_error is False
    assert "10" in _tool_text(stats)
    assert "+100.0% vs previous (5)" in _tool_text(stats)
    assert ambiguous.is_error is True
    assert "Example Docs" in _tool_text(ambiguous)
    assert broken.is_error is True
    assert "date range" in _tool_text(broken).lower()


@pytest.mark.asyncio
async def test_stdio_process_lists_tools() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "umami_mcp"],
        cwd=ROOT,
        env={"UMAMI_API_KEY": "test-key", "MCP_TRANSPORT": "stdio"},
    )
    async with Client(params) as client:
        listed = await client.list_tools()
    assert [tool.name for tool in listed.tools] == EXPECTED_TOOLS


@pytest.mark.asyncio
async def test_http_process_requires_bearer_and_lists_tools() -> None:
    port = _free_port()
    log = tempfile.TemporaryFile()
    process = subprocess.Popen(
        [sys.executable, "-m", "umami_mcp", "http"],
        cwd=ROOT,
        env={
            "UMAMI_API_KEY": "test-key",
            "MCP_AUTH_TOKEN": "mcp-secret",
            "MCP_HOST": "127.0.0.1",
            "PORT": str(port),
            "PATH": "/usr/bin:/bin",
        },
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_health(port, process, log)
        health = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        anonymous = httpx.post(
            f"http://127.0.0.1:{port}/mcp",
            headers={"content-type": "application/json", "accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            timeout=2,
        )
        assert anonymous.status_code == 401
        wrong = httpx.get(
            f"http://127.0.0.1:{port}/mcp",
            headers={"authorization": "Bearer wrong-token"},
            timeout=2,
        )
        assert wrong.status_code == 401

        async with httpx2.AsyncClient(
            headers={"Authorization": "Bearer mcp-secret"},
            timeout=httpx2.Timeout(30.0, read=30.0),
        ) as http_client:
            transport = streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http_client)
            async with Client(transport) as client:
                listed = await client.list_tools()
        assert [tool.name for tool in listed.tools] == EXPECTED_TOOLS
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()


def _wait_for_health(port: int, process: subprocess.Popen[bytes], log: tempfile.TemporaryFile) -> None:
    deadline = time.time() + 20
    url = f"http://127.0.0.1:{port}/health"
    while time.time() < deadline:
        if process.poll() is not None:
            raise AssertionError(_read_log(log) or f"server exited {process.returncode}")
        try:
            response = httpx.get(url, timeout=0.5)
        except httpx.HTTPError:
            time.sleep(0.1)
            continue
        if response.status_code == 200:
            return
        time.sleep(0.1)
    raise AssertionError(_read_log(log) or "timed out waiting for /health")


def _read_log(log: tempfile.TemporaryFile) -> str:
    log.seek(0)
    return log.read().decode(errors="replace")
