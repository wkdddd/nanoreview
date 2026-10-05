"""Network security utilities — SSRF protection and internal URL detection."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import threading
import weakref
from contextlib import contextmanager, suppress
from typing import Any, cast
from urllib.parse import urlparse
from urllib.request import getproxies, proxy_bypass

import httpx

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),   # carrier-grade NAT
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local / cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::/128"),            # unspecified; may route to local host
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),          # unique local
    ipaddress.ip_network("fe80::/10"),         # link-local v6
]

_URL_RE = re.compile(r"https?://[^\s\"'`;|<>]+", re.IGNORECASE)
_allowed_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []


def configure_ssrf_whitelist(cidrs: list[str]) -> None:
    """Allow specific CIDR ranges to bypass SSRF blocking (e.g. Tailscale's 100.64.0.0/10)."""
    global _allowed_networks
    nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in cidrs:
        with suppress(ValueError):
            nets.append(ipaddress.ip_network(cidr, strict=False))
    _allowed_networks = nets


def _normalize_addr(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Normalize IPv6-mapped IPv4 addresses to their IPv4 form.

    ``::ffff:127.0.0.1`` is semantically identical to ``127.0.0.1`` but
    Python's ipaddress treats it as an IPv6Address that matches neither
    ``127.0.0.0/8`` nor ``::1/128``.  Converting it to IPv4 ensures
    blocklist/allowlist checks work correctly.
    """
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _is_private(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    normalized = _normalize_addr(addr)
    if _allowed_networks and any(normalized in net for net in _allowed_networks):
        return False
    return any(normalized in net for net in _BLOCKED_NETWORKS)


def resolve_url_target(
    url: str,
    *,
    allow_loopback: bool = False,
) -> tuple[bool, str, tuple[str, ...]]:
    """Validate a URL is safe to fetch: scheme, hostname, and resolved IPs.

    ``allow_loopback`` is intentionally narrow: it only permits literal
    loopback hosts (localhost, 127.0.0.0/8, ::1) when every resolved address is
    loopback. It does not allow RFC1918, link-local, metadata, or public DNS
    names that happen to resolve to loopback.

    Returns (ok, error_message, resolved_ips).  When ok is True,
    resolved_ips contains the addresses validated for this URL, so callers can
    pin DNS instead of re-resolving at connect time.
    """
    try:
        p = urlparse(url)
    except Exception as e:
        return False, str(e), ()

    if p.scheme not in ("http", "https"):
        return False, f"Only http/https allowed, got '{p.scheme or 'none'}'", ()
    if not p.netloc:
        return False, "Missing domain", ()
    if not p.hostname:
        return False, "Missing hostname", ()

    try:
        infos = socket.getaddrinfo(p.hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        return False, f"Cannot resolve hostname: {p.hostname}", ()

    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        addrs.append(addr)
    if allow_loopback and _is_allowed_loopback_target(p.hostname, addrs):
        return True, "", tuple(dict.fromkeys(str(_normalize_addr(addr)) for addr in addrs))
    for addr in addrs:
        if _is_private(addr):
            return False, f"Blocked: {p.hostname} resolves to private/internal address {addr}", ()

    return True, "", tuple(dict.fromkeys(str(_normalize_addr(addr)) for addr in addrs))


def validate_url_target(url: str, *, allow_loopback: bool = False) -> tuple[bool, str]:
    """Validate a URL is safe to fetch: scheme, hostname, and resolved IPs."""
    ok, error, _ = resolve_url_target(url, allow_loopback=allow_loopback)
    return ok, error


def env_proxy_applies_to_url(url: str) -> bool:
    """Return True when process proxy settings would proxy this URL.

    Mirrors :func:`httpx_env_proxy_mounts`: hosts allowed by the SSRF policy are
    exempted here too, so callers that skip a direct reachability probe because
    "the proxy will handle it" agree with the transport that is actually used.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False

    proxies = getproxies()
    proxy_url = proxies.get(parsed.scheme) or proxies.get("all")
    if not proxy_url:
        return False

    host = parsed.hostname
    with suppress(ValueError):
        addr = ipaddress.ip_address(host.strip("[]"))
        if _allowed_networks and any(addr in net for net in _allowed_networks):
            return False

    if parsed.port is not None:
        host = f"[{host}]:{parsed.port}" if ":" in host else f"{host}:{parsed.port}"
    return not proxy_bypass(host)


def httpx_env_proxy_mounts() -> dict[str, httpx.AsyncBaseTransport | None]:
    """Build HTTPX proxy mounts while leaving direct routes to the base transport.

    Environment proxies (``HTTP_PROXY``/``HTTPS_PROXY``) are honoured, but any
    host that this module considers a legitimate direct target is exempted with
    a ``None`` mount. Without that exemption a loopback MCP server is sent
    through the proxy even after ``tools.ssrf_whitelist`` allowed it, which
    shows up as the proxy answering ``404`` for the SSE message endpoint.
    """
    proxies = getproxies()
    mounts: dict[str, httpx.AsyncBaseTransport | None] = {}
    for scheme in ("http", "https", "all"):
        proxy_url = proxies.get(scheme)
        if proxy_url:
            if "://" not in proxy_url:
                proxy_url = f"http://{proxy_url}"
            mounts[f"{scheme}://"] = httpx.AsyncHTTPTransport(proxy=httpx.Proxy(proxy_url))

    if not mounts:
        return {}

    # Hosts the SSRF policy already allows must bypass the proxy, otherwise a
    # whitelisted private/loopback endpoint would still be routed externally.
    # HTTPX mounts match on host (optionally a "*suffix"), not on CIDR, so the
    # loopback literals are exempted explicitly.
    if _allowed_networks:
        for loopback in ("127.0.0.1", "localhost", "[::1]"):
            mounts[f"all://{loopback}"] = None

    no_proxy = proxies.get("no", "")
    if no_proxy == "*":
        return {}
    for entry in no_proxy.split(","):
        pattern = _no_proxy_mount_pattern(entry.strip())
        if pattern:
            mounts[pattern] = None
    return mounts


def _no_proxy_mount_pattern(hostname: str) -> str | None:
    if not hostname:
        return None
    if "://" in hostname:
        return hostname

    unbracketed = hostname.strip("[]")
    with suppress(ValueError):
        addr = ipaddress.ip_address(unbracketed)
        return f"all://[{addr}]" if addr.version == 6 else f"all://{addr}"

    if hostname.lower() == "localhost":
        return "all://localhost"
    return f"all://*{hostname}"


#: ``socket.getaddrinfo`` is process-global, so the pin has to be guarded by a
#: thread-level lock rather than an ``asyncio.Lock``: one process may run
#: several event loops (tests, SDK embedding), and an asyncio lock would bind
#: to whichever loop first awaited it and then raise
#: "bound to a different event loop" for every other loop.
_resolver_lock = threading.RLock()
#: Active pin count, so only the outermost context restores the real resolver.
_resolver_depth = [0]
#: Per-event-loop pin locks; weak keys so closed loops are not kept alive.
_loop_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


@contextmanager
def pin_resolved_url_dns(url: str, resolved_ips: tuple[str, ...]):
    """Pin DNS lookups for the URL hostname to previously validated IPs.

    This temporarily overrides process-global resolver state, so concurrent
    callers are serialized by a re-entrant lock and the original resolver is
    only restored by the outermost context. Do not call it directly for HTTP
    requests; prefer PinnedDNSAsyncTransport, which also holds that lock
    across the request.
    """
    try:
        hostname = urlparse(url).hostname
    except Exception:
        hostname = None
    if not hostname or not resolved_ips:
        yield
        return

    pinned_host = hostname.rstrip(".").lower()
    with _resolver_lock:
        depth = _resolver_depth[0]
        original_getaddrinfo = socket.getaddrinfo
        _resolver_depth[0] = depth + 1

    def _getaddrinfo(
        host: Any,
        port: Any,
        family: int = 0,
        type: int = 0,  # noqa: A002
        proto: int = 0,
        flags: int = 0,
    ) -> list[Any]:
        if str(host).rstrip(".").lower() != pinned_host:
            return original_getaddrinfo(host, port, family, type, proto, flags)
        infos: list[Any] = []
        for ip in resolved_ips:
            addr = ipaddress.ip_address(ip)
            addr_family = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
            if family not in (0, socket.AF_UNSPEC, addr_family):
                continue
            sockaddr = (ip, port or 0, 0, 0) if addr_family == socket.AF_INET6 else (ip, port or 0)
            infos.append((addr_family, type or socket.SOCK_STREAM, proto, "", sockaddr))
        return infos

    socket.getaddrinfo = cast(Any, _getaddrinfo)
    try:
        yield
    finally:
        with _resolver_lock:
            _resolver_depth[0] -= 1
            # Only the outermost context restores the real resolver; an inner
            # exit must not unpin a request that is still in flight.
            if _resolver_depth[0] == 0:
                socket.getaddrinfo = original_getaddrinfo


class UnsafeURLRequestError(httpx.RequestError):
    """Raised when an outgoing request is rejected by URL safety validation."""


class PinnedDNSAsyncTransport(httpx.AsyncBaseTransport):
    """HTTPX transport that pins each request to the IPs validated for its URL."""

    def __init__(
        self,
        *,
        allow_loopback: bool = False,
        inner: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._allow_loopback = allow_loopback
        self._inner = inner or httpx.AsyncHTTPTransport()

    @staticmethod
    def _loop_lock() -> asyncio.Lock:
        """Return the pin lock of the running loop, creating it on demand.

        A single shared ``asyncio.Lock`` would bind to the first loop that
        awaited it, so any later loop would fail with "bound to a different
        event loop". Keying by loop keeps requests within one loop serialized
        (the global resolver is process-wide) while staying usable from every
        loop in the process.
        """
        loop = asyncio.get_running_loop()
        with _resolver_lock:
            lock = _loop_locks.get(loop)
            if lock is None:
                lock = _loop_locks[loop] = asyncio.Lock()
            return lock

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        # Resolution calls socket.getaddrinfo, which is blocking. Keep it off
        # the event loop: a slow resolver would otherwise stall every other
        # session (including /stop handling) for the duration of the lookup.
        ok, error, resolved_ips = await asyncio.to_thread(
            resolve_url_target, url, allow_loopback=self._allow_loopback
        )
        if not ok:
            raise UnsafeURLRequestError(error, request=request)
        async with self._loop_lock():
            with pin_resolved_url_dns(url, resolved_ips):
                return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def validate_resolved_url(url: str) -> tuple[bool, str]:
    """Validate an already-fetched URL (e.g. after redirect). Only checks the IP, skips DNS."""
    try:
        p = urlparse(url)
    except Exception:
        return True, ""

    hostname = p.hostname
    if not hostname:
        return True, ""

    try:
        addr = ipaddress.ip_address(hostname)
        if _is_private(addr):
            return False, f"Redirect target is a private address: {addr}"
    except ValueError:
        # hostname is a domain name, resolve it
        try:
            infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            return True, ""
        for info in infos:
            try:
                addr = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if _is_private(addr):
                return False, f"Redirect target {hostname} resolves to private address {addr}"

    return True, ""


def contains_internal_url(command: str, *, allow_loopback: bool = False) -> bool:
    """Return True if the command string contains a URL targeting an internal/private address."""
    for m in _URL_RE.finditer(command):
        url = m.group(0)
        ok, _ = validate_url_target(url, allow_loopback=allow_loopback)
        if not ok:
            return True
    return False


def _is_allowed_loopback_target(
    hostname: str,
    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address],
) -> bool:
    if not addrs or not all(_normalize_addr(addr).is_loopback for addr in addrs):
        return False
    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost":
        return True
    with suppress(ValueError):
        return ipaddress.ip_address(hostname).is_loopback
    return False
