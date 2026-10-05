"""Smoke-check the MCP client against a real local MCP server.

Runs one stdio server plus SSE and Streamable HTTP servers from a single local
fixture, so all three transports are exercised against the actual MCP SDK rather
than a fake. The HTTP servers bind to loopback and therefore need the SSRF
whitelist configured explicitly.

Skipped when the optional HTTP stack (``uvicorn``) is unavailable.

Run with::

    pytest tests/agent/tools/test_mcp_smoke.py -q
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from nanoreview.agent.tools.mcp import (
    MCPProvider,
    _close_mcp_connections,
    connect_mcp_servers,
)
from nanoreview.agent.tools.registry import ToolRegistry
from nanoreview.config.schema import MCPServerConfig
from nanoreview.security import network as network_module

_uvicorn_available = importlib.util.find_spec("uvicorn") is not None

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _bound_port(server: Any) -> int | None:
    """Read the port uvicorn actually bound, or ``None`` before startup."""
    for running in server.servers:
        for sock in running.sockets:
            return int(sock.getsockname()[1])
    return None


def _endpoint_ready(label: str, port: int, timeout: float = 20.0) -> bool:
    """Wait until the transport's own endpoint answers, not just the socket.

    ``server.started`` only reports that the socket is bound; the ASGI lifespan
    that backs the routes may still be starting. For SSE the probe opens the
    stream and closes it again, which is harmless because the MCP client always
    establishes its own session.
    """
    import httpx

    path = "/sse" if label == "sse" else "/mcp"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if label == "sse":
                with httpx.stream(
                    "GET", f"http://127.0.0.1:{port}{path}", timeout=2.0
                ) as response:
                    if response.status_code == 200:
                        return True
            else:
                response = httpx.post(
                    f"http://127.0.0.1:{port}{path}",
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    headers={"Accept": "application/json, text/event-stream"},
                    timeout=5.0,
                )
                # Any answer other than "route missing" proves the app serves.
                if response.status_code != 404:
                    return True
        except Exception:
            pass
        time.sleep(0.05)
    return False


@pytest.fixture
def stdio_server_script(tmp_path: Path) -> Path:
    script = tmp_path / "smoke_server.py"
    script.write_text(
        "from mcp.server.fastmcp import FastMCP\n"
        "\n"
        "server = FastMCP('smoke-stdio')\n"
        "\n"
        "@server.tool()\n"
        "def echo(text: str) -> str:\n"
        "    '''Return the given text.'''\n"
        "    return f'stdio:{text}'\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    server.run()\n",
        encoding="utf-8",
    )
    return script


@pytest.fixture
def local_http_servers():
    """Start SSE and Streamable HTTP MCP servers on loopback."""
    if not _uvicorn_available:
        pytest.skip("uvicorn is required to host the local HTTP MCP servers")

    from mcp.server.fastmcp import FastMCP
    from uvicorn import Config, Server

    def _make_server(label: str) -> FastMCP:
        server = FastMCP(f"smoke-{label}")

        @server.tool()
        def echo(text: str) -> str:
            """Return the given text."""
            return f"{label}:{text}"

        return server

    apps = {
        "sse": _make_server("sse").sse_app(),
        "streamableHttp": _make_server("streamableHttp").streamable_http_app(),
    }

    servers: list[Any] = []
    threads: list[threading.Thread] = []
    urls: dict[str, str] = {}
    try:
        for label, app in apps.items():
            # ``server.started`` only means the socket is bound; the ASGI
            # lifespan (which starts the MCP session manager and therefore the
            # /messages route the SSE client posts to) may still be starting.
            # Readiness is decided by an actual request to the transport's own
            # endpoint, and the bound port is read back rather than assumed, so
            # a client can never silently reach the wrong app.
            for _attempt in range(5):
                port = _free_port()
                server = Server(
                    Config(app, host="127.0.0.1", port=port, log_level="error")
                )
                thread = threading.Thread(
                    target=server.run, daemon=True, name=f"smoke-{label}"
                )
                thread.start()
                deadline = time.monotonic() + 15.0
                while time.monotonic() < deadline and not server.started:
                    if not thread.is_alive():
                        break
                    time.sleep(0.05)
                bound = _bound_port(server) if server.started else None
                if bound is not None and _endpoint_ready(label, bound):
                    servers.append(server)
                    threads.append(thread)
                    urls[label] = f"http://127.0.0.1:{bound}"
                    break
                with contextlib.suppress(Exception):
                    server.should_exit = True
                thread.join(timeout=5)
            else:
                pytest.skip(f"local {label} MCP server did not become ready")

        yield urls
    finally:
        for server in servers:
            with contextlib.suppress(Exception):
                server.should_exit = True
        for thread in threads:
            thread.join(timeout=5)


@pytest.fixture
def allow_loopback():
    """Permit loopback HTTP for the duration of one smoke test."""
    original = list(network_module._allowed_networks)
    network_module.configure_ssrf_whitelist(["127.0.0.0/8"])
    try:
        yield
    finally:
        network_module._allowed_networks = original


class TestSmokeStdio:
    @pytest.mark.asyncio
    async def test_stdio_server_tool_round_trip(self, stdio_server_script: Path):
        registry = ToolRegistry()
        connections = await connect_mcp_servers(
            {
                "local": MCPServerConfig(
                    type="stdio",
                    command=sys.executable,
                    args=[str(stdio_server_script)],
                    tool_timeout=30,
                )
            },
            registry,
        )

        try:
            assert set(connections) == {"local"}
            tool_name = "mcp_local_echo"
            assert registry.has(tool_name)
            result = await registry.execute(tool_name, {"text": "hi"})
            assert "stdio:hi" in str(result)
        finally:
            await _close_mcp_connections(connections)


class TestSmokeHTTP:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("transport", ["sse", "streamableHttp"])
    async def test_http_server_tool_round_trip(
        self, local_http_servers, allow_loopback, transport: str
    ):
        base = local_http_servers[transport]
        suffix = "/sse" if transport == "sse" else "/mcp"
        registry = ToolRegistry()
        connections = await connect_mcp_servers(
            {
                "remote": MCPServerConfig(
                    type=transport,
                    url=f"{base}{suffix}",
                    tool_timeout=30,
                )
            },
            registry,
        )

        try:
            assert set(connections) == {"remote"}
            tool_name = "mcp_remote_echo"
            assert registry.has(tool_name)
            result = await registry.execute(tool_name, {"text": "hi"})
            assert f"{transport}:hi" in str(result)
        finally:
            await _close_mcp_connections(connections)

    @pytest.mark.asyncio
    async def test_headers_auth_reaches_the_server(
        self, local_http_servers, allow_loopback
    ):
        base = local_http_servers["streamableHttp"]
        provider = MCPProvider(
            {
                "remote": MCPServerConfig(
                    type="streamableHttp",
                    url=f"{base}/mcp",
                    headers={"Authorization": "Bearer smoke-token"},
                    tool_timeout=30,
                )
            }
        )

        await provider.connect()

        try:
            assert provider.connected_server_names == {"remote"}
            assert provider.runtime_status()["remote"] == "connected"
            turn_registry = ToolRegistry()
            assert provider.build_turn_proxies(turn_registry) >= 1
            result = await turn_registry.execute("mcp_remote_echo", {"text": "auth"})
            assert "streamableHttp:auth" in str(result)
        finally:
            await provider.aclose()


class TestSmokeLoopbackGuard:
    @pytest.mark.asyncio
    async def test_local_server_is_refused_without_the_whitelist(
        self, local_http_servers
    ):
        base = local_http_servers["streamableHttp"]
        registry = ToolRegistry()

        connections = await connect_mcp_servers(
            {
                "remote": MCPServerConfig(
                    type="streamableHttp", url=f"{base}/mcp", tool_timeout=10
                )
            },
            registry,
        )

        try:
            # Loopback is blocked by default: an operator must opt in through
            # tools.ssrfWhitelist before a local MCP server is reachable.
            assert connections == {}
            assert registry.tool_names == []
        finally:
            await _close_mcp_connections(connections)


class TestSmokeConnectionRecovery:
    @pytest.mark.asyncio
    async def test_provider_reconnect_refreshes_the_live_wrapper(
        self, stdio_server_script: Path
    ):
        provider = MCPProvider(
            {
                "local": MCPServerConfig(
                    type="stdio",
                    command=sys.executable,
                    args=[str(stdio_server_script)],
                    tool_timeout=30,
                )
            }
        )

        await provider.connect()
        try:
            assert provider.connected_server_names == {"local"}
            turn_registry = ToolRegistry()
            assert provider.build_turn_proxies(turn_registry) == 1

            first = await turn_registry.execute("mcp_local_echo", {"text": "one"})
            assert "stdio:one" in str(first)

            # Simulate a terminated session: drop the wrapper and force the
            # provider to rebuild the connection, then keep using the same
            # in-flight turn registry.
            stale = provider.registry.get("mcp_local_echo")
            provider.registry.unregister("mcp_local_echo")
            await provider._close_server("local")

            await provider.connect()

            assert provider.connected_server_names == {"local"}
            assert provider.registry.get("mcp_local_echo") is not stale
            second = await turn_registry.execute("mcp_local_echo", {"text": "two"})
            assert "stdio:two" in str(second)
        finally:
            await provider.aclose()

    @pytest.mark.asyncio
    async def test_shutdown_leaves_no_owner_task_behind(
        self, stdio_server_script: Path
    ):
        provider = MCPProvider(
            {
                "local": MCPServerConfig(
                    type="stdio",
                    command=sys.executable,
                    args=[str(stdio_server_script)],
                    tool_timeout=30,
                )
            }
        )

        await provider.connect()
        owner_names = {
            task.get_name()
            for task in asyncio.all_tasks()
            if task.get_name().startswith("mcp:")
        }
        assert owner_names

        await provider.aclose()
        await asyncio.sleep(0)

        remaining = {
            task.get_name()
            for task in asyncio.all_tasks()
            if task.get_name().startswith("mcp:")
        }
        assert remaining == set()
        assert provider.connected_server_names == set()
