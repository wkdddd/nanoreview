"""Contract for the shared network guards used by agent HTTP tools.

The DNS pin mutates process-global ``socket.getaddrinfo``, so its mutual
exclusion has to hold across every event loop in the process. A single
``asyncio.Lock`` binds to whichever loop first awaited it and raises
"bound to a different event loop" everywhere else, which only shows up once a
second loop exists (test runs, SDK embedding, one loop per thread).
"""

from __future__ import annotations

import asyncio
import socket
import threading

import httpx
import pytest

from nanoreview.security import network as network_module
from nanoreview.security.network import (
    PinnedDNSAsyncTransport,
    UnsafeURLRequestError,
    pin_resolved_url_dns,
    resolve_url_target,
)


@pytest.fixture(autouse=True)
def _restore_resolver_state():
    original = socket.getaddrinfo
    allowed = list(network_module._allowed_networks)
    yield
    socket.getaddrinfo = original
    network_module._resolver_depth[0] = 0
    # The SSRF whitelist is process-global too; leaking it makes later tests
    # observe a policy they never configured.
    network_module.configure_ssrf_whitelist(
        [str(net) for net in allowed]
    )


class _RecordingTransport:
    """Inner transport that resolves the host while the pin is installed."""

    def __init__(self) -> None:
        self.resolved: list[list[str]] = []

    async def handle_async_request(self, request):
        host = request.url.host
        infos = socket.getaddrinfo(host, 80, type=socket.SOCK_STREAM)
        self.resolved.append([str(info[4][0]) for info in infos])
        return httpx.Response(200, request=request)

    async def aclose(self) -> None:
        return None


class TestPinnedTransportAcrossEventLoops:
    def test_same_transport_serves_requests_from_separate_loops(self):
        """The regression: a shared asyncio.Lock fails on the second loop."""
        network_module.configure_ssrf_whitelist(["127.0.0.0/8"])
        transport = PinnedDNSAsyncTransport(allow_loopback=True)
        inner = _RecordingTransport()
        transport._inner = inner  # type: ignore[assignment]

        results: list[list[str]] = []
        for _ in range(3):
            # A fresh loop per iteration, as separate sessions would produce.
            loop = asyncio.new_event_loop()
            try:
                request = httpx.Request("GET", "http://localhost:8931/mcp")
                response = loop.run_until_complete(transport.handle_async_request(request))
                assert response.status_code == 200
                results.append(inner.resolved[-1])
            finally:
                loop.close()

        # Every loop must reach the inner transport, and each request must be
        # pinned to the validated loopback address (v4 and v6 both allowed).
        assert len(results) == 3
        for resolved in results:
            assert resolved
            assert set(resolved) <= {"127.0.0.1", "::1"}
        # The real resolver must be back, not a leftover pin.
        assert network_module._resolver_depth[0] == 0

    async def test_blocked_target_raises_before_any_request(self):
        network_module.configure_ssrf_whitelist([])
        transport = PinnedDNSAsyncTransport(inner=_RecordingTransport())

        request = httpx.Request("GET", "http://169.254.169.254/latest/meta-data")
        with pytest.raises(UnsafeURLRequestError):
            await transport.handle_async_request(request)
        assert network_module._resolver_depth[0] == 0

    async def test_slow_resolution_does_not_block_the_event_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A slow resolver must not stall unrelated coroutines.

        ``resolve_url_target`` calls ``socket.getaddrinfo``, which is blocking.
        Running it inline on the loop freezes every other session -- including
        ``/stop`` handling -- for the whole lookup. The transport must hand the
        lookup to a worker thread.

        The fake resolver holds for 250ms, released by a plain timer thread so
        the gate does not depend on the loop making progress. A short ticker is
        expected to finish well before the lookup returns: if the resolve ran
        inline, the ticker would only start after the gate opened.
        """
        release = threading.Event()
        threading.Timer(0.25, release.set).start()

        def _slow_resolve(url: str, *, allow_loopback: bool = False):
            release.wait(timeout=5)
            return True, "", ["127.0.0.1"]

        monkeypatch.setattr(network_module, "resolve_url_target", _slow_resolve)
        network_module.configure_ssrf_whitelist(["127.0.0.0/8"])
        transport = PinnedDNSAsyncTransport(
            allow_loopback=True, inner=_RecordingTransport()
        )

        ticker_done_at: list[float] = []
        loop = asyncio.get_running_loop()
        started = loop.time()

        async def _ticker() -> None:
            # 5 ticks * 10ms = 50ms, comfortably inside the 250ms gate.
            for _ in range(5):
                await asyncio.sleep(0.01)
            ticker_done_at.append(loop.time() - started)

        async def _run_request() -> None:
            await transport.handle_async_request(
                httpx.Request("GET", "http://localhost:8931/mcp")
            )

        await asyncio.gather(_run_request(), _ticker())

        assert ticker_done_at, "ticker never ran"
        # The lookup held for 250ms. A blocked loop would push the ticker past
        # that; a decoupled one lets it finish in ~50ms.
        assert ticker_done_at[0] < 0.2, f"ticker starved: {ticker_done_at[0]:.3f}s"
        assert network_module._resolver_depth[0] == 0


class TestPinNesting:
    def test_inner_exit_does_not_restore_the_resolver_early(self):
        sentinel = socket.getaddrinfo
        with pin_resolved_url_dns("http://localhost:1/", ("127.0.0.1",)):
            with pin_resolved_url_dns("http://localhost:1/", ("127.0.0.1",)):
                pinned = socket.getaddrinfo
                assert pinned is not sentinel
            # Still pinned: the outer context is still active.
            assert socket.getaddrinfo is pinned
        assert socket.getaddrinfo is sentinel
        assert network_module._resolver_depth[0] == 0

    def test_url_without_hostname_is_a_no_op(self):
        sentinel = socket.getaddrinfo
        with pin_resolved_url_dns("not-a-url", ("127.0.0.1",)):
            assert socket.getaddrinfo is sentinel
        assert network_module._resolver_depth[0] == 0

    def test_empty_resolved_ips_is_a_no_op(self):
        sentinel = socket.getaddrinfo
        with pin_resolved_url_dns("http://localhost:1/", ()):
            assert socket.getaddrinfo is sentinel
        assert network_module._resolver_depth[0] == 0


class TestResolveUrlTarget:
    def test_loopback_requires_explicit_allowance(self):
        ok, error, _ips = resolve_url_target("http://127.0.0.1:8931/mcp")
        assert ok is False
        assert error

        ok, error, ips = resolve_url_target(
            "http://127.0.0.1:8931/mcp", allow_loopback=True
        )
        assert ok is True, error
        assert "127.0.0.1" in ips

    def test_localhost_resolves_to_loopback(self):
        ok, _error, ips = resolve_url_target("http://localhost:8931/mcp", allow_loopback=True)
        assert ok is True
        assert ips

    def test_metadata_address_is_blocked(self):
        ok, error, _ips = resolve_url_target("http://169.254.169.254/latest")
        assert ok is False
        assert error


class TestEnvProxyExemption:
    """A whitelisted loopback endpoint must not be routed through the proxy.

    Without the exemption an environment proxy answers the MCP SSE message
    endpoint with 404, because only the SSE stream itself was reaching the
    local server.
    """

    @pytest.fixture(autouse=True)
    def _proxy_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:50925")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:50925")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)

    def test_proxy_is_used_when_nothing_is_whitelisted(self):
        network_module.configure_ssrf_whitelist([])
        mounts = network_module.httpx_env_proxy_mounts()

        assert isinstance(mounts.get("http://"), httpx.AsyncHTTPTransport)
        assert "all://127.0.0.1" not in mounts
        assert network_module.env_proxy_applies_to_url("http://127.0.0.1:8931/sse")

    def test_whitelisted_loopback_is_exempted_from_the_proxy(self):
        network_module.configure_ssrf_whitelist(["127.0.0.0/8"])
        mounts = network_module.httpx_env_proxy_mounts()

        # None means "use the base transport", i.e. connect directly.
        assert mounts.get("all://127.0.0.1") is None
        assert mounts.get("all://localhost") is None
        assert not network_module.env_proxy_applies_to_url(
            "http://127.0.0.1:8931/messages/?session_id=x"
        )

    def test_exempted_loopback_is_still_validated_for_dns_pinning(self):
        network_module.configure_ssrf_whitelist(["127.0.0.0/8"])
        ok, _error, ips = resolve_url_target("http://127.0.0.1:8931/mcp")
        assert ok is True
        assert ips

    def test_no_proxy_star_disables_all_mounts(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("NO_PROXY", "*")
        network_module.configure_ssrf_whitelist([])

        assert network_module.httpx_env_proxy_mounts() == {}
