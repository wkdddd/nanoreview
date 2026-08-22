"""WebSocket server channel: nanobot acts as a WebSocket server and serves connected clients."""

from __future__ import annotations

import asyncio
import base64
import binascii
import email.utils
import hashlib
import hmac
import http
import json
import mimetypes
import re
import secrets
import shutil
import ssl
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import parse_qs, unquote, urlparse

from aiohttp import web
from loguru import logger
from pydantic import Field, field_validator, model_validator
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request as WsRequest
from websockets.http11 import Response

from nanoreview.bus.events import OUTBOUND_META_AGENT_UI, InboundMessage, OutboundMessage
from nanoreview.bus.queue import MessageBus
from nanoreview.channels.base import BaseChannel
from nanoreview.command.builtin import builtin_command_palette
from nanoreview.config.paths import get_media_dir
from nanoreview.config.schema import Base, Config
from nanoreview.review import normalize_review_action, normalize_review_target_type
from nanoreview.review.input import parse_repo_target
from nanoreview.utils.helpers import safe_filename
from nanoreview.utils.media_decode import (
    FileSizeExceeded,
    save_base64_data_url,
)
from nanoreview.utils.subagent_channel_display import scrub_subagent_messages_for_channel
from nanoreview.utils.webui_thread_disk import delete_webui_thread
from nanoreview.utils.webui_transcript import append_transcript_object, build_webui_thread_response
from nanoreview.utils.webui_turn_helpers import websocket_turn_wall_started_at

if TYPE_CHECKING:
    from nanoreview.session.manager import SessionManager


_CODE_CONTEXT_DEFAULT_BEFORE = 8
_CODE_CONTEXT_DEFAULT_AFTER = 12
_CODE_CONTEXT_MAX_WINDOW = 80
_CODE_CONTEXT_MAX_BYTES = 1_000_000


class CodeContextError(Exception):
    """HTTP-shaped error for WebUI code context lookups."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _strip_trailing_slash(path: str) -> str:
    if len(path) > 1 and path.endswith("/"):
        return path.rstrip("/")
    return path or "/"


def _normalize_config_path(path: str) -> str:
    return _strip_trailing_slash(path)


def _review_mode_payload(meta: dict[str, Any]) -> dict[str, Any]:
    target = str(meta.get("review_target") or "").strip()
    target_type = normalize_review_target_type(
        str(meta.get("review_target_type") or ""),
        target or None,
    )
    payload: dict[str, Any] = {}
    if target:
        payload["target"] = target
    if target_type:
        payload["target_type"] = target_type
    action = str(meta.get("review_action") or "").strip()
    if action:
        payload["action"] = action
    focus = meta.get("review_focus")
    if isinstance(focus, list):
        payload["focus"] = focus
    elif isinstance(focus, str) and focus.strip():
        payload["focus"] = [item.strip() for item in focus.split(",") if item.strip()]
    return payload


def _clear_review_metadata(meta: dict[str, Any]) -> None:
    for key in (
        "review_target",
        "review_target_type",
        "review_focus",
        "review_action",
        "review_mode_variant",
        "review_mode_name",
        "review_max_subagents",
    ):
        meta.pop(key, None)


class WebSocketConfig(Base):
    """WebSocket server channel configuration.

    Clients connect with URLs like ``ws://{host}:{port}{path}?client_id=...&token=...``.
    - ``client_id``: Used for ``allow_from`` authorization; if omitted, a value is generated and logged.
    - ``token``: If non-empty, the ``token`` query param may match this static secret; short-lived tokens
      from ``token_issue_path`` are also accepted.
    - ``token_issue_path``: If non-empty, **GET** (HTTP/1.1) to this path returns JSON
      ``{"token": "...", "expires_in": <seconds>}``; use ``?token=...`` when opening the WebSocket.
      Must differ from ``path`` (the WS upgrade path). If the client runs in the **same process** as
      nanobot and shares the asyncio loop, use a thread or async HTTP client for GET—do not call
      blocking ``urllib`` or synchronous ``httpx`` from inside a coroutine.
    - ``token_issue_secret``: If non-empty, token requests must send ``Authorization: Bearer <secret>`` or
      ``X-Nanobot-Auth: <secret>``.
    - ``websocket_requires_token``: If True, the handshake must include a valid token (static or issued and not expired).
    - Each connection has its own session: a unique ``chat_id`` maps to the agent session internally.
    - ``media`` field in outbound messages contains local filesystem paths; remote clients need a
      shared filesystem or an HTTP file server to access these files.
    """

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8765
    path: str = "/"
    token: str = ""
    token_issue_path: str = ""
    token_issue_secret: str = ""
    token_ttl_s: int = Field(default=300, ge=30, le=86_400)
    websocket_requires_token: bool = True
    allow_from: list[str] = Field(default_factory=lambda: ["*"])
    streaming: bool = True
    # Default 36 MB, upper 40 MB: supports up to 4 images at ~6 MB each after
    # client-side Worker normalization (see webui Composer). 4 × 6 MB × 1.37
    # (base64 overhead) + envelope framing stays under 36 MB; the 40 MB ceiling
    # leaves a small margin for sender slop without opening a DoS avenue.
    max_message_bytes: int = Field(default=37_748_736, ge=1024, le=41_943_040)
    ping_interval_s: float = Field(default=20.0, ge=5.0, le=300.0)
    ping_timeout_s: float = Field(default=20.0, ge=5.0, le=300.0)
    ssl_certfile: str = ""
    ssl_keyfile: str = ""

    @field_validator("path")
    @classmethod
    def path_must_start_with_slash(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError('path must start with "/"')
        return _normalize_config_path(value)

    @field_validator("token_issue_path")
    @classmethod
    def token_issue_path_format(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if not value.startswith("/"):
            raise ValueError('token_issue_path must start with "/"')
        return _normalize_config_path(value)

    @model_validator(mode="after")
    def token_issue_path_differs_from_ws_path(self) -> Self:
        if not self.token_issue_path:
            return self
        if _normalize_config_path(self.token_issue_path) == _normalize_config_path(self.path):
            raise ValueError("token_issue_path must differ from path (the WebSocket upgrade path)")
        return self

    @model_validator(mode="after")
    def wildcard_host_requires_auth(self) -> Self:
        if self.host not in ("0.0.0.0", "::"):
            return self
        if self.token.strip() or self.token_issue_secret.strip():
            return self
        raise ValueError(
            "host is 0.0.0.0 (all interfaces) but neither token nor "
            "token_issue_secret is set — set one to prevent unauthenticated access"
        )


def _http_json_response(data: dict[str, Any], *, status: int = 200) -> Response:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    headers = Headers(
        [
            ("Date", email.utils.formatdate(usegmt=True)),
            ("Connection", "close"),
            ("Content-Length", str(len(body))),
            ("Content-Type", "application/json; charset=utf-8"),
        ]
    )
    reason = http.HTTPStatus(status).phrase
    return Response(status, reason, headers, body)


def publish_runtime_model_update(
    bus: MessageBus,
    model: str,
    model_preset: str | None,
) -> None:
    """Enqueue a runtime model snapshot for websocket subscribers (fan-out in-channel)."""
    bus.outbound.put_nowait(OutboundMessage(
        channel="websocket",
        chat_id="*",
        content="",
        metadata={
            "_runtime_model_updated": True,
            "model": model,
            "model_preset": model_preset,
        },
    ))


def _default_model_name_from_config() -> str | None:
    """Resolved model string from on-disk config (bootstrap fallback)."""
    try:
        from nanoreview.config.loader import load_config

        model = load_config().resolve_preset().model.strip()
        return model or None
    except Exception as e:
        logger.debug("bootstrap model_name could not load from config: {}", e)
        return None


def _resolve_bootstrap_model_name(
    runtime_name: Callable[[], str | None] | None,
) -> str | None:
    """Prefer an in-process resolver (e.g. AgentLoop); else config-derived default."""
    if runtime_name is not None:
        try:
            raw = runtime_name()
        except Exception as e:
            logger.debug("bootstrap runtime model resolver failed: {}", e)
        else:
            if isinstance(raw, str):
                stripped = raw.strip()
                if stripped:
                    return stripped
    return _default_model_name_from_config()


def _parse_request_path(path_with_query: str) -> tuple[str, dict[str, list[str]]]:
    """Parse normalized path and query parameters in one pass."""
    parsed = urlparse("ws://x" + path_with_query)
    path = _strip_trailing_slash(parsed.path or "/")
    return path, parse_qs(parsed.query, keep_blank_values=True)


def _normalize_http_path(path_with_query: str) -> str:
    """Return the path component (no query string), with trailing slash normalized (root stays ``/``)."""
    return _parse_request_path(path_with_query)[0]


def _parse_query(path_with_query: str) -> dict[str, list[str]]:
    return _parse_request_path(path_with_query)[1]


def _query_first(query: dict[str, list[str]], key: str) -> str | None:
    """Return the first value for *key*, or None."""
    values = query.get(key)
    return values[0] if values else None


def _mask_secret_hint(secret: str | None) -> str | None:
    if not secret:
        return None
    if len(secret) <= 8:
        return "••••"
    return f"{secret[:4]}••••{secret[-4:]}"




def _parse_inbound_payload(raw: str) -> str | None:
    """Parse a client frame into text; return None for empty or unrecognized content."""
    text = raw.strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(data, dict):
            for key in ("content", "text", "message"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value
            return None
        return None
    return text


# Accept UUIDs and short scoped keys like "unified:default". Keeps the capability
# namespace small enough to rule out path traversal / quote injection tricks.
_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_:-]{1,64}$")


def _is_valid_chat_id(value: Any) -> bool:
    return isinstance(value, str) and _CHAT_ID_RE.match(value) is not None


def _parse_envelope(raw: str) -> dict[str, Any] | None:
    """Return a typed envelope dict if the frame is a new-style JSON envelope, else None.

    A frame qualifies when it parses as a JSON object with a string ``type`` field.
    Legacy frames (plain text, or ``{"content": ...}`` without ``type``) return None;
    callers should fall back to :func:`_parse_inbound_payload` for those.
    """
    text = raw.strip()
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    t = data.get("type")
    if not isinstance(t, str):
        return None
    return data


# Per-message media limits. The server-side guard is a touch looser than the
# client's ``Worker`` normalization target (6 MB) — tolerate client slop, but
# still cap total ingress at ``_MAX_IMAGES_PER_MESSAGE * _MAX_IMAGE_BYTES``
# which fits comfortably inside ``max_message_bytes``.
_MAX_IMAGES_PER_MESSAGE = 4
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_VIDEOS_PER_MESSAGE = 1
_MAX_VIDEO_BYTES = 20 * 1024 * 1024

# Image MIME whitelist — matches the Composer's ``accept`` list. SVG is
# explicitly excluded to avoid the XSS surface inside embedded scripts.
_IMAGE_MIME_ALLOWED: frozenset[str] = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
})

_VIDEO_MIME_ALLOWED: frozenset[str] = frozenset({
    "video/mp4",
    "video/webm",
    "video/quicktime",
})

_UPLOAD_MIME_ALLOWED: frozenset[str] = _IMAGE_MIME_ALLOWED | _VIDEO_MIME_ALLOWED

_DATA_URL_MIME_RE = re.compile(r"^data:([^;]+);base64,", re.DOTALL)


def _extract_data_url_mime(url: str) -> str | None:
    """Return the MIME type of a ``data:<mime>;base64,...`` URL, else ``None``."""
    if not isinstance(url, str):
        return None
    m = _DATA_URL_MIME_RE.match(url)
    if not m:
        return None
    return m.group(1).strip().lower() or None


_LOCALHOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# Matches the legacy chat-id pattern but allows file-system-safe stems too,
# so the API can address sessions whose keys came from non-WebSocket channels.
_API_KEY_RE = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")


def _decode_api_key(raw_key: str) -> str | None:
    """Decode a percent-encoded API path segment, then validate the result."""
    key = unquote(raw_key)
    if _API_KEY_RE.match(key) is None:
        return None
    return key


def _is_localhost(connection: Any) -> bool:
    """Return True if *connection* originated from the loopback interface."""
    addr = getattr(connection, "remote_address", None)
    if not addr:
        return False
    host = addr[0] if isinstance(addr, tuple) else addr
    if not isinstance(host, str):
        return False
    # ``::ffff:127.0.0.1`` is loopback in IPv6-mapped form.
    if host.startswith("::ffff:"):
        host = host[7:]
    return host in _LOCALHOSTS


def _http_response(
    body: bytes,
    *,
    status: int = 200,
    content_type: str = "text/plain; charset=utf-8",
    extra_headers: list[tuple[str, str]] | None = None,
) -> Response:
    headers = [
        ("Date", email.utils.formatdate(usegmt=True)),
        ("Connection", "close"),
        ("Content-Length", str(len(body))),
        ("Content-Type", content_type),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    reason = http.HTTPStatus(status).phrase
    return Response(status, reason, Headers(headers), body)


def _http_error(status: int, message: str | None = None) -> Response:
    body = (message or http.HTTPStatus(status).phrase).encode("utf-8")
    return _http_response(body, status=status)


def _bearer_token(headers: Any) -> str | None:
    """Pull a Bearer token out of standard or query-style headers."""
    auth = headers.get("Authorization") or headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


def _is_websocket_upgrade(request: WsRequest) -> bool:
    """Detect an actual WS upgrade; plain HTTP GETs to the same path should fall through."""
    upgrade = request.headers.get("Upgrade") or request.headers.get("upgrade")
    connection = request.headers.get("Connection") or request.headers.get("connection")
    if not upgrade or "websocket" not in upgrade.lower():
        return False
    if not connection or "upgrade" not in connection.lower():
        return False
    return True


def _is_aiohttp_websocket_upgrade(request: web.Request) -> bool:
    """Detect a real aiohttp WebSocket upgrade before applying WS auth gates."""
    upgrade = request.headers.get("Upgrade", "")
    connection = request.headers.get("Connection", "")
    if "websocket" not in upgrade.lower():
        return False
    if "upgrade" not in connection.lower():
        return False
    return True


def _b64url_encode(data: bytes) -> str:
    """URL-safe base64 without padding — compact + friendly in URL paths."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    """Reverse of :func:`_b64url_encode`; caller handles ``ValueError``."""
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# Allowed MIME types we actually serve from the media endpoint. Anything
# outside this set is degraded to ``application/octet-stream`` so an
# attacker who somehow gets a signed URL for an unexpected file type can't
# trick the browser into sniffing executable content.
_MEDIA_ALLOWED_MIMES: frozenset[str] = frozenset({
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "video/mp4",
    "video/webm",
    "video/quicktime",
})


def _issue_route_secret_matches(headers: Any, configured_secret: str) -> bool:
    """Return True if the token-issue HTTP request carries credentials matching ``token_issue_secret``."""
    if not configured_secret:
        return True
    authorization = headers.get("Authorization") or headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
        return hmac.compare_digest(supplied, configured_secret)
    header_token = headers.get("X-Nanobot-Auth") or headers.get("x-nanobot-auth")
    if not header_token:
        return False
    return hmac.compare_digest(header_token.strip(), configured_secret)


class _AiohttpConnection:
    """Small adapter so aiohttp websockets can reuse channel send helpers."""

    def __init__(self, request: web.Request, ws: web.WebSocketResponse) -> None:
        self.request = request
        self.ws = ws
        self.remote_address = (request.remote or "", 0)

    async def send(self, raw: str) -> None:
        await self.ws.send_str(raw)


class WebSocketChannel(BaseChannel):
    """Run a local WebSocket server; forward text/JSON messages to the message bus."""

    name = "websocket"
    display_name = "WebSocket"

    def __init__(
        self,
        config: Any,
        bus: MessageBus,
        *,
        root_config: Config | None = None,
        session_manager: "SessionManager | None" = None,
        static_dist_path: Path | None = None,
        runtime_model_name: Callable[[], str | None] | None = None,
        runtime_usage: Callable[[], dict[str, Any]] | None = None,
    ):
        if isinstance(config, dict):
            config = WebSocketConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: WebSocketConfig = config
        # chat_id -> connections subscribed to it (fan-out target).
        self._subs: dict[str, set[Any]] = {}
        # connection -> chat_ids it is subscribed to (O(1) cleanup on disconnect).
        self._conn_chats: dict[Any, set[str]] = {}
        # connection -> default chat_id for legacy frames that omit routing.
        self._conn_default: dict[Any, str] = {}
        # Single-use tokens consumed at WebSocket handshake.
        self._issued_tokens: dict[str, float] = {}
        # Multi-use tokens for HTTP routes served beside WS; checked but not consumed.
        self._api_tokens: dict[str, float] = {}
        self._stop_event: asyncio.Event | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._aiohttp_runner: web.AppRunner | None = None
        self._session_manager = session_manager
        self._static_dist_path: Path | None = (
            static_dist_path.resolve() if static_dist_path is not None else None
        )
        self._runtime_model_name = runtime_model_name
        self._runtime_usage = runtime_usage
        self._root_config = root_config
        # Process-local secret used to HMAC-sign media URLs. The signed URL is
        # the capability — anyone who holds a valid URL can fetch that one
        # file, nothing else. The secret regenerates on restart so links
        # become self-expiring (callers just refresh the session list).
        self._media_secret: bytes = secrets.token_bytes(32)

    # -- Subscription bookkeeping -------------------------------------------

    def _attach(self, connection: Any, chat_id: str) -> None:
        """Idempotently subscribe *connection* to *chat_id*."""
        self._subs.setdefault(chat_id, set()).add(connection)
        self._conn_chats.setdefault(connection, set()).add(chat_id)

    async def _cleanup_connection(self, connection: Any) -> None:
        """Remove *connection* from every subscription set; safe to call multiple times."""
        chat_ids = self._conn_chats.pop(connection, set())
        orphaned_chats: list[str] = []
        for cid in chat_ids:
            subs = self._subs.get(cid)
            if subs is None:
                continue
            subs.discard(connection)
            if not subs:
                self._subs.pop(cid, None)
                orphaned_chats.append(cid)
        self._conn_default.pop(connection, None)
        for cid in orphaned_chats:
            await self.bus.publish_inbound(
                InboundMessage(
                    channel="websocket",
                    chat_id=cid,
                    sender_id="",
                    content="",
                    metadata={"_permission_disconnect": True},
                )
            )

    async def _maybe_push_turn_run_wall_clock(self, chat_id: str) -> None:
        """Replay ``goal_status: running`` when a turn is still active (same-process refresh)."""
        t0 = websocket_turn_wall_started_at(chat_id)
        if t0 is None:
            return
        await self.send_goal_status(chat_id, "running", started_at=t0)

    async def _maybe_push_session_approval_state(self, chat_id: str) -> None:
        """Replay session approval toggle after subscribe so refreshes restore it."""
        if self._session_manager is None:
            return
        row = self._session_manager.read_session_file(f"websocket:{chat_id}")
        meta = row.get("metadata", {}) if isinstance(row, dict) else {}
        if not isinstance(meta, dict):
            meta = {}
        perms = meta.get("permissions", {})
        if not isinstance(perms, dict):
            return
        approval_enabled = bool(perms.get("approval_enabled", False))
        if not approval_enabled:
            return
        conns = list(self._subs.get(chat_id, ()))
        for conn in conns:
            await self._send_event(
                conn,
                "session_permission_updated",
                chat_id=chat_id,
                approval_enabled=approval_enabled,
            )

    async def _maybe_push_specialist_modes(self, chat_id: str) -> None:
        """Replay mutually exclusive mode toggles after subscribe."""
        if self._session_manager is None:
            return
        row = self._session_manager.read_session_file(f"websocket:{chat_id}")
        meta = row.get("metadata", {}) if isinstance(row, dict) else {}
        if not isinstance(meta, dict):
            return
        review_enabled = bool(meta.get("review_mode", False))
        conns = list(self._subs.get(chat_id, ()))
        for conn in conns:
            if review_enabled:
                await self._send_event(
                    conn,
                    "review_mode_updated",
                    chat_id=chat_id,
                    enabled=review_enabled,
                    **_review_mode_payload(meta),
                )

    async def _hydrate_after_subscribe(self, chat_id: str) -> None:
        """Replay goal/run strip state after subscribe (same-process refresh)."""
        await self._maybe_push_turn_run_wall_clock(chat_id)
        await self._maybe_push_session_approval_state(chat_id)
        await self._maybe_push_specialist_modes(chat_id)

    async def _send_event(self, connection: Any, event: str, **fields: Any) -> None:
        """Send a control event (attached, error, ...) to a single connection."""
        payload: dict[str, Any] = {"event": event}
        payload.update(fields)
        raw = json.dumps(payload, ensure_ascii=False)
        try:
            await connection.send(raw)
        except ConnectionClosed:
            await self._cleanup_connection(connection)
        except Exception as e:
            self.logger.warning("failed to send {} event: {}", event, e)

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return WebSocketConfig().model_dump(by_alias=True)

    def _expected_path(self) -> str:
        return _normalize_config_path(self.config.path)

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        cert = self.config.ssl_certfile.strip()
        key = self.config.ssl_keyfile.strip()
        if not cert and not key:
            return None
        if not cert or not key:
            raise ValueError(
                "ssl_certfile and ssl_keyfile must both be set for WSS, or both left empty"
            )
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile=cert, keyfile=key)
        return ctx

    _MAX_ISSUED_TOKENS = 10_000

    def _purge_expired_issued_tokens(self) -> None:
        now = time.monotonic()
        for token_key, expiry in list(self._issued_tokens.items()):
            if now > expiry:
                self._issued_tokens.pop(token_key, None)

    def _take_issued_token_if_valid(self, token_value: str | None) -> bool:
        """Validate and consume one issued token (single use per connection attempt).

        Uses single-step pop to minimize the window between lookup and removal;
        safe under asyncio's single-threaded cooperative model.
        """
        if not token_value:
            return False
        self._purge_expired_issued_tokens()
        expiry = self._issued_tokens.pop(token_value, None)
        if expiry is None:
            return False
        if time.monotonic() > expiry:
            return False
        return True

    def _handle_token_issue_http(self, connection: Any, request: Any) -> Any:
        secret = self.config.token_issue_secret.strip()
        if secret:
            if not _issue_route_secret_matches(request.headers, secret):
                return connection.respond(401, "Unauthorized")
        else:
            self.logger.warning(
                "token_issue_path is set but token_issue_secret is empty; "
                "any client can obtain connection tokens — set token_issue_secret for production."
            )
        self._purge_expired_issued_tokens()
        if len(self._issued_tokens) >= self._MAX_ISSUED_TOKENS:
            self.logger.error(
                "too many outstanding issued tokens ({}), rejecting issuance",
                len(self._issued_tokens),
            )
            return _http_json_response({"error": "too many outstanding tokens"}, status=429)
        token_value = f"nbwt_{secrets.token_urlsafe(32)}"
        self._issued_tokens[token_value] = time.monotonic() + float(self.config.token_ttl_s)

        return _http_json_response(
            {"token": token_value, "expires_in": self.config.token_ttl_s}
        )

    # -- HTTP dispatch ------------------------------------------------------

    async def _dispatch_http(self, connection: Any, request: WsRequest) -> Any:
        """Route an inbound HTTP request to a handler or to the WS upgrade path."""
        got, query = _parse_request_path(request.path)

        # 1. Token issue endpoint (legacy, optional, gated by configured secret).
        if self.config.token_issue_path:
            issue_expected = _normalize_config_path(self.config.token_issue_path)
            if got == issue_expected:
                return self._handle_token_issue_http(connection, request)

        # 2. Bootstrap (`/webui/bootstrap`): mint WS/API tokens + shared session metadata.
        if got == "/webui/bootstrap":
            return self._handle_bootstrap(connection, request)

        # 3. REST handlers co-located with this channel (sessions, settings, …).
        if got == "/api/sessions":
            return self._handle_sessions_list(request)

        if got == "/api/settings":
            return self._handle_settings(request)

        if got == "/api/usage":
            return self._handle_usage(request)

        if got == "/api/commands":
            return self._handle_commands(request)

        if got == "/api/settings/update":
            return self._handle_settings_update(request)

        if got == "/api/settings/provider/update":
            return self._handle_settings_provider_update(request)

        if got == "/api/settings/web-search/update":
            return self._handle_settings_web_search_update(request)

        m = re.match(r"^/api/sessions/([^/]+)/messages$", got)
        if m:
            return self._handle_session_messages(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/webui-thread$", got)
        if m:
            return self._handle_webui_thread_get(request, m.group(1))

        m = re.match(r"^/api/sessions/([^/]+)/code-context$", got)
        if m:
            return self._handle_code_context_get(request, m.group(1))

        # NOTE: websockets' HTTP parser only accepts GET, so we cannot expose a
        # true ``DELETE`` verb. The action is folded into the path instead.
        m = re.match(r"^/api/sessions/([^/]+)/delete$", got)
        if m:
            return self._handle_session_delete(request, m.group(1))

        # Session metadata update (pin, rename, etc.).  Same GET-only
        # constraint as delete — the payload is sent as a query/body param.
        m = re.match(r"^/api/sessions/([^/]+)/update$", got)
        if m:
            return self._handle_session_update(request, m.group(1))

        # Signed media fetch: ``<sig>`` is an HMAC over ``<payload>``; the
        # payload decodes to a path inside :func:`get_media_dir`. See
        # :meth:`_sign_media_path` for the inverse direction used to build
        # these URLs when replaying a session.
        m = re.match(r"^/api/media/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)$", got)
        if m:
            return self._handle_media_fetch(m.group(1), m.group(2))

        # 4. WebSocket upgrade (the channel's primary purpose). Only run the
        # handshake gate on requests that actually ask to upgrade; otherwise
        # a bare ``GET /`` from the browser would be rejected as an
        # unauthorized WS handshake instead of serving the SPA's index.html.
        expected_ws = self._expected_path()
        if got == expected_ws and _is_websocket_upgrade(request):
            client_id = _query_first(query, "client_id") or ""
            if len(client_id) > 128:
                client_id = client_id[:128]
            if not self.is_allowed(client_id):
                return connection.respond(403, "Forbidden")
            return self._authorize_websocket_handshake(connection, query)

        # 5. Static SPA serving (only if a build directory was wired in).
        if self._static_dist_path is not None:
            response = self._serve_static(got)
            if response is not None:
                return response

        return connection.respond(404, "Not Found")

    # -- HTTP route handlers ------------------------------------------------

    def _check_api_token(self, request: WsRequest) -> bool:
        """Validate a request against the API token pool (multi-use, TTL-bound)."""
        self._purge_expired_api_tokens()
        token = _bearer_token(request.headers) or _query_first(
            _parse_query(request.path), "token"
        )
        if not token:
            return False
        expiry = self._api_tokens.get(token)
        if expiry is None or time.monotonic() > expiry:
            self._api_tokens.pop(token, None)
            return False
        return True

    def _check_api_token_value(self, token: str | None) -> bool:
        self._purge_expired_api_tokens()
        if not token:
            return False
        expiry = self._api_tokens.get(token)
        if expiry is None or time.monotonic() > expiry:
            self._api_tokens.pop(token, None)
            return False
        return True

    @staticmethod
    def _aiohttp_bearer(request: web.Request) -> str | None:
        return _bearer_token(request.headers) or request.query.get("token")

    def _check_aiohttp_api_token(self, request: web.Request) -> bool:
        return self._check_api_token_value(self._aiohttp_bearer(request))

    def _issue_bootstrap_token_payload(self) -> dict[str, Any] | None:
        self._purge_expired_issued_tokens()
        self._purge_expired_api_tokens()
        if (
            len(self._issued_tokens) >= self._MAX_ISSUED_TOKENS
            or len(self._api_tokens) >= self._MAX_ISSUED_TOKENS
        ):
            return None
        token = f"nbwt_{secrets.token_urlsafe(32)}"
        expiry = time.monotonic() + float(self.config.token_ttl_s)
        self._issued_tokens[token] = expiry
        self._api_tokens[token] = expiry
        return {
            "token": token,
            "ws_path": self._expected_path(),
            "expires_in": self.config.token_ttl_s,
            "model_name": _resolve_bootstrap_model_name(self._runtime_model_name),
        }

    def _purge_expired_api_tokens(self) -> None:
        now = time.monotonic()
        for token_key, expiry in list(self._api_tokens.items()):
            if now > expiry:
                self._api_tokens.pop(token_key, None)

    def _handle_bootstrap(self, connection: Any, request: Any) -> Response:
        # When a secret is configured (token_issue_secret or static token),
        # validate it regardless of source IP.  This secures deployments
        # behind a reverse proxy where all connections appear as localhost.
        secret = self.config.token_issue_secret.strip() or self.config.token.strip()
        if secret:
            if not _issue_route_secret_matches(request.headers, secret):
                return _http_error(401, "Unauthorized")
        elif not _is_localhost(connection):
            # No secret configured: only allow localhost (local dev mode).
            return _http_error(403, "bootstrap is localhost-only")
        payload = self._issue_bootstrap_token_payload()
        if payload is None:
            return _http_response(
                json.dumps({"error": "too many outstanding tokens"}).encode("utf-8"),
                status=429,
                content_type="application/json; charset=utf-8",
            )
        return _http_json_response(payload)

    def _handle_sessions_list(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self._session_manager is None:
            return _http_error(503, "session manager unavailable")
        sessions = self._session_manager.list_sessions()
        # Sidebar/chat listing for WS-backed sessions only — CLI / Slack / etc.
        # keys are not intended for resume over this HTTP surface.
        cleaned = [
            {k: v for k, v in s.items() if k != "path"}
            for s in sessions
            if isinstance(s.get("key"), str) and s["key"].startswith("websocket:")
        ]
        return _http_json_response({"sessions": cleaned})

    def _settings_payload(self, *, requires_restart: bool = False) -> dict[str, Any]:
        from nanoreview.config.loader import get_config_path, load_config
        from nanoreview.providers.registry import PROVIDERS, find_by_name

        config = load_config()
        defaults = config.agents.defaults
        provider_name = config.get_provider_name(defaults.model) or defaults.provider
        provider = config.get_provider(defaults.model)
        selected_provider = provider_name
        if defaults.provider != "auto":
            spec = find_by_name(defaults.provider)
            selected_provider = spec.name if spec else provider_name
        providers = []
        for spec in PROVIDERS:
            provider_config = getattr(config.providers, spec.name, None)
            if provider_config is None or spec.is_oauth or spec.is_local:
                continue
            providers.append(
                {
                    "name": spec.name,
                    "label": spec.label,
                    "configured": bool(provider_config.api_key),
                    "api_key_hint": _mask_secret_hint(provider_config.api_key),
                    "api_base": provider_config.api_base,
                    "default_api_base": spec.default_api_base or None,
                }
            )
        return {
            "agent": {
                "model": defaults.model,
                "provider": selected_provider,
                "resolved_provider": provider_name,
                "has_api_key": bool(provider and provider.api_key),
            },
            "providers": providers,
            "runtime": {
                "config_path": str(get_config_path().expanduser()),
            },
            "requires_restart": requires_restart,
        }

    def _handle_settings(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(self._settings_payload())

    @staticmethod
    def _normalize_usage_payload(usage: dict[str, Any] | None) -> dict[str, int]:
        raw = usage or {}
        normalized: dict[str, int] = {}
        for key, value in raw.items():
            try:
                normalized[key] = int(value or 0)
            except (TypeError, ValueError):
                continue
        normalized["prompt_tokens"] = normalized.get("prompt_tokens", 0)
        normalized["completion_tokens"] = normalized.get("completion_tokens", 0)
        if "total_tokens" not in normalized:
            normalized["total_tokens"] = sum(
                value
                for key, value in normalized.items()
                if key.endswith("_tokens") and key != "total_tokens"
            )
        return normalized

    def _usage_payload(self) -> dict[str, Any]:
        runtime = self._runtime_usage() if self._runtime_usage is not None else {}
        total_usage = self._normalize_usage_payload(runtime.get("usage"))
        last_usage = self._normalize_usage_payload(runtime.get("last_usage"))
        return {
            "scope": "process",
            "usage": total_usage,
            "last_usage": last_usage,
            "started_at": runtime.get("started_at"),
            "note": "Subagent usage is not additionally included in the global total.",
        }

    def _handle_usage(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response(self._usage_payload())

    def _handle_commands(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_json_response({"commands": builtin_command_palette()})

    def _handle_settings_update(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from nanoreview.config.loader import load_config, save_config
        from nanoreview.providers.registry import find_by_name

        query = _parse_query(request.path)
        config = load_config()
        defaults = config.agents.defaults
        changed = False

        model = _query_first(query, "model")
        if model is not None:
            model = model.strip()
            if not model:
                return _http_error(400, "model is required")
            if defaults.model != model:
                defaults.model = model
                changed = True

        provider = _query_first(query, "provider")
        if provider is not None:
            provider = provider.strip()
            if not provider:
                return _http_error(400, "provider is required")
            if find_by_name(provider) is None:
                return _http_error(400, "unknown provider")
            provider_config = getattr(config.providers, provider, None)
            if provider_config is None or not provider_config.api_key:
                return _http_error(400, "provider is not configured")
            if defaults.provider != provider:
                defaults.provider = provider
                changed = True

        if changed:
            save_config(config)
        # LLM provider/model changes are hot-reloaded by AgentLoop before each
        # new turn via the provider snapshot loader, so a restart is unnecessary.
        return _http_json_response(self._settings_payload(requires_restart=False))

    def _handle_settings_provider_update(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        from nanoreview.config.loader import load_config, save_config
        from nanoreview.providers.registry import find_by_name

        query = _parse_query(request.path)
        provider_name = (_query_first(query, "provider") or "").strip()
        if not provider_name:
            return _http_error(400, "provider is required")
        spec = find_by_name(provider_name)
        if spec is None or spec.is_oauth or spec.is_local:
            return _http_error(400, "unknown provider")

        config = load_config()
        provider_config = getattr(config.providers, spec.name, None)
        if provider_config is None:
            return _http_error(400, "unknown provider")

        changed = False
        if "api_key" in query or "apiKey" in query:
            api_key = _query_first(query, "api_key")
            if api_key is None:
                api_key = _query_first(query, "apiKey")
            api_key = (api_key or "").strip() or None
            if provider_config.api_key != api_key:
                provider_config.api_key = api_key
                changed = True

        if "api_base" in query or "apiBase" in query:
            api_base = _query_first(query, "api_base")
            if api_base is None:
                api_base = _query_first(query, "apiBase")
            api_base = (api_base or "").strip() or None
            if provider_config.api_base != api_base:
                provider_config.api_base = api_base
                changed = True

        if changed:
            save_config(config)
        # API key/base changes are picked up by the next provider snapshot refresh.
        return _http_json_response(self._settings_payload(requires_restart=False))

    def _handle_settings_web_search_update(self, request: WsRequest) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        return _http_error(410, "web search has been removed")

    @staticmethod
    def _is_websocket_channel_session_key(key: str) -> bool:
        """True when *key* is a ``websocket:…`` session exposed on this HTTP surface."""
        return key.startswith("websocket:")

    def _handle_session_messages(self, request: WsRequest, key: str) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self._session_manager is None:
            return _http_error(503, "session manager unavailable")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        # Only ``websocket:…`` sessions are listed/served here — same boundary as
        # ``/api/sessions``. Block handcrafted URLs from probing CLI / Slack / etc.
        if not self._is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        data = self._session_manager.read_session_file(decoded_key)
        if data is None:
            return _http_error(404, "session not found")
        messages = data.get("messages")
        if isinstance(messages, list):
            scrub_subagent_messages_for_channel(messages)
        # Decorate persisted user messages with signed media URLs so the
        # client can render previews. The raw on-disk ``media`` paths are
        # stripped on the way out — they leak server filesystem layout and
        # the client never needs them once it has the signed fetch URL.
        self._augment_media_urls(data)
        return _http_json_response(data)

    def _handle_webui_thread_get(self, request: WsRequest, key: str) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not self._is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        data = build_webui_thread_response(
            decoded_key,
            augment_user_media=self._augment_transcript_user_media,
        )
        if data is None:
            return _http_error(404, "webui thread not found")
        return _http_json_response(data)

    def _handle_code_context_get(self, request: WsRequest, key: str) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        query = _parse_query(request.path)
        try:
            payload = self._code_context_payload(
                decoded_key,
                file_path=_query_first(query, "file") or "",
                line=self._int_query_value(_query_first(query, "line"), 1, min_value=1, max_value=10_000_000),
                before=self._int_query_value(
                    _query_first(query, "before"),
                    _CODE_CONTEXT_DEFAULT_BEFORE,
                    min_value=0,
                    max_value=_CODE_CONTEXT_MAX_WINDOW,
                ),
                after=self._int_query_value(
                    _query_first(query, "after"),
                    _CODE_CONTEXT_DEFAULT_AFTER,
                    min_value=0,
                    max_value=_CODE_CONTEXT_MAX_WINDOW,
                ),
            )
        except CodeContextError as exc:
            return _http_error(exc.status, exc.message)
        return _http_json_response(payload)

    def _webui_sessions_payload(self) -> dict[str, Any]:
        if self._session_manager is None:
            raise RuntimeError("session manager unavailable")
        sessions = self._session_manager.list_sessions()
        cleaned = [
            {k: v for k, v in s.items() if k != "path"}
            for s in sessions
            if isinstance(s.get("key"), str) and s["key"].startswith("websocket:")
        ]
        return {"sessions": cleaned}

    def _session_messages_payload(self, key: str) -> dict[str, Any] | None:
        if self._session_manager is None:
            raise RuntimeError("session manager unavailable")
        if not self._is_websocket_channel_session_key(key):
            return None
        data = self._session_manager.read_session_file(key)
        if data is None:
            return None
        messages = data.get("messages")
        if isinstance(messages, list):
            scrub_subagent_messages_for_channel(messages)
        self._augment_media_urls(data)
        return data

    def _webui_thread_payload(self, key: str) -> dict[str, Any] | None:
        if not self._is_websocket_channel_session_key(key):
            return None
        return build_webui_thread_response(
            key,
            augment_user_media=self._augment_transcript_user_media,
        )

    def _workspace_root(self) -> Path:
        if self._root_config is not None:
            return self._root_config.workspace_path.resolve()
        if self._session_manager is not None:
            return self._session_manager.workspace.resolve()
        return Path.cwd().resolve()

    @staticmethod
    def _normal_code_rel_path(path: str) -> str:
        cleaned = path.strip().replace("\\", "/")
        if not cleaned:
            raise CodeContextError(400, "file is required")
        pure = Path(cleaned)
        if pure.is_absolute() or cleaned.startswith("/") or cleaned.startswith("../") or "/../" in cleaned:
            raise CodeContextError(400, "file must be a relative path")
        if cleaned in {".", ".."}:
            raise CodeContextError(400, "file must be a relative file path")
        return cleaned

    @staticmethod
    def _int_query_value(value: str | None, default: int, *, min_value: int, max_value: int) -> int:
        if value is None or not value.strip():
            return default
        try:
            parsed = int(value)
        except ValueError as exc:
            raise CodeContextError(400, "line, before, and after must be integers") from exc
        return max(min_value, min(max_value, parsed))

    @staticmethod
    def _read_utf8_context(
        target: Path,
        *,
        file_label: str,
        line: int,
        before: int,
        after: int,
    ) -> dict[str, Any]:
        if not target.is_file():
            raise CodeContextError(404, "file not found")
        try:
            size = target.stat().st_size
        except OSError as exc:
            raise CodeContextError(500, "failed to stat file") from exc
        if size > _CODE_CONTEXT_MAX_BYTES:
            raise CodeContextError(413, "file is too large for preview")
        try:
            raw = target.read_bytes()
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CodeContextError(415, "file is not UTF-8 text") from exc
        except OSError as exc:
            raise CodeContextError(500, "failed to read file") from exc
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        total = len(lines)
        safe_line = max(1, min(line, total or 1))
        start_line = max(1, safe_line - before)
        end_line = min(total, safe_line + after)
        code = "\n".join(lines[start_line - 1:end_line])
        return {
            "file": file_label,
            "line": safe_line,
            "startLine": start_line,
            "endLine": end_line,
            "code": code,
            "truncated": start_line > 1 or end_line < total,
        }

    def _resolve_local_code_file(self, metadata: dict[str, Any], rel_path: str) -> Path:
        workspace = self._workspace_root()
        target_value = str(metadata.get("review_target") or "").strip()

        if target_value:
            raw_target = Path(target_value).expanduser()
            target_root = raw_target if raw_target.is_absolute() else workspace / raw_target
            try:
                resolved_root = target_root.resolve()
                if resolved_root.is_file():
                    # review_target 是单个文件：检查 rel_path 是否指向它
                    normalized = rel_path.replace("\\", "/")
                    target_posix = resolved_root.as_posix()
                    if target_posix.endswith("/" + normalized) or target_posix == normalized:
                        return resolved_root
                elif resolved_root.is_dir():
                    candidate = (resolved_root / rel_path).resolve()
                    candidate.relative_to(resolved_root)
                    if candidate.is_file():
                        return candidate
            except (OSError, ValueError) as exc:
                logger.debug("code_context.target_branch_failed rel_path={} target={} error={}", rel_path, target_value, exc)

        try:
            root = workspace.resolve()
            candidate = (root / rel_path).resolve()
            candidate.relative_to(root)
            if candidate.is_file():
                return candidate
        except (OSError, ValueError) as exc:
            logger.debug("code_context.workspace_branch_failed rel_path={} workspace={} error={}", rel_path, workspace, exc)

        raise CodeContextError(404, "file not found")

    def _resolve_github_snapshot_file(self, metadata: dict[str, Any], rel_path: str) -> Path:
        workspace = self._workspace_root()
        cache_root = workspace / ".nanoreview" / "review_github"
        if not cache_root.is_dir():
            raise CodeContextError(404, "review snapshot not found")
        raw_target = str(metadata.get("review_target") or "").strip()
        target = parse_repo_target(raw_target) or raw_target
        candidates: list[Path] = []
        for manifest in cache_root.glob("*/.nanoreview_snapshot.json"):
            snapshot_dir = manifest.parent
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            files = data.get("files")
            snapshot_name = str(data.get("snapshot") or "")
            manifest_files = {str(item).replace("\\", "/") for item in files} if isinstance(files, list) else set()
            if manifest_files and rel_path not in manifest_files:
                continue
            if target and target not in snapshot_name and snapshot_name not in target:
                continue
            candidates.insert(0, snapshot_dir)
        for snapshot_dir in candidates:
            try:
                root = snapshot_dir.resolve()
                root.relative_to(cache_root.resolve())
                candidate = (root / rel_path).resolve()
                candidate.relative_to(root)
            except (OSError, ValueError):
                continue
            if candidate.is_file():
                return candidate
        raise CodeContextError(404, "file not found in review snapshot")

    def _code_context_payload(
        self,
        key: str,
        *,
        file_path: str,
        line: int,
        before: int,
        after: int,
    ) -> dict[str, Any]:
        if self._session_manager is None:
            raise CodeContextError(503, "session manager unavailable")
        if not self._is_websocket_channel_session_key(key):
            raise CodeContextError(404, "session not found")
        data = self._session_manager.read_session_file(key)
        if data is None:
            raise CodeContextError(404, "session not found")
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        # 如果前端传了绝对路径，先尝试转成相对路径（workspace 或 review_target 内）
        _raw = file_path.strip()
        if _raw:
            _p = Path(_raw)
            if _p.is_absolute() or "/" in _raw and _raw.startswith("/"):
                _ws = self._workspace_root()
                try:
                    _abs = _p.resolve() if _p.is_absolute() else (_ws / _raw.lstrip("/")).resolve()
                    _abs.relative_to(_ws)
                    _raw = _abs.relative_to(_ws).as_posix()
                except (ValueError, OSError):
                    # 不在 workspace 内，尝试 review_target
                    target_value = str(metadata.get("review_target") or "").strip()
                    if target_value:
                        _t = Path(target_value).expanduser()
                        _t_root = _t if _t.is_absolute() else _ws / _t
                        try:
                            _t_resolved = _t_root.resolve()
                            if _t_resolved.is_file():
                                _t_parent = _t_resolved.parent
                            else:
                                _t_parent = _t_resolved
                            _abs.relative_to(_t_parent)
                            _raw = _abs.relative_to(_t_parent).as_posix()
                        except (ValueError, OSError):
                            pass
        rel_path = self._normal_code_rel_path(_raw)
        target_type = normalize_review_target_type(
            str(metadata.get("review_target_type") or ""),
            str(metadata.get("review_target") or "") or None,
        )
        if target_type == "github":
            target = self._resolve_github_snapshot_file(metadata, rel_path)
            source = "github_snapshot"
        else:
            target = self._resolve_local_code_file(metadata, rel_path)
            source = "local"
        payload = self._read_utf8_context(
            target,
            file_label=rel_path,
            line=line,
            before=before,
            after=after,
        )
        payload["source"] = source
        return payload

    def _delete_session_key(self, key: str) -> bool | None:
        if self._session_manager is None:
            raise RuntimeError("session manager unavailable")
        if not self._is_websocket_channel_session_key(key):
            return None
        deleted = self._session_manager.delete_session(key)
        delete_webui_thread(key)
        return bool(deleted)

    def _try_append_webui_transcript(self, chat_id: str, wire: dict[str, Any]) -> None:
        sk = f"websocket:{chat_id}"
        try:
            dup = json.loads(json.dumps(wire, ensure_ascii=False))
            if isinstance(dup, dict):
                dup.setdefault("createdAt", int(time.time() * 1000))
                review_target = str(dup.get("review_target") or "").strip()
                review_target_type = str(dup.get("review_target_type") or "").strip()
                review_mode_variant = str(dup.get("review_mode_variant") or "").strip()
                review_action = str(dup.get("review_action") or "").strip()
                review_focus = dup.get("review_focus")
                if review_target or review_target_type or review_mode_variant or review_action or review_focus:
                    review: dict[str, Any] = {}
                    if review_target:
                        review["target"] = review_target
                    if review_target_type:
                        review["target_type"] = review_target_type
                    if review_mode_variant:
                        review["mode"] = review_mode_variant
                    if review_action:
                        review["action"] = review_action
                    if isinstance(review_focus, list):
                        review["focus"] = review_focus
                    dup["review"] = review
            append_transcript_object(sk, dup)
        except (OSError, ValueError, TypeError) as e:
            self.logger.warning("webui transcript append failed: {}", e)

    def _augment_transcript_user_media(self, paths: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for pstr in paths:
            path = Path(pstr)
            att = self._sign_or_stage_media_path(path)
            if att is None:
                continue
            mime, _ = mimetypes.guess_type(path.name)
            kind = "video" if mime and mime.startswith("video/") else "image"
            out.append(
                {"kind": kind, "url": att["url"], "name": att.get("name", path.name)},
            )
        return out

    async def _handle_message(
        self,
        sender_id: str,
        chat_id: str,
        content: str,
        media: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_key: str | None = None,
        is_dm: bool = False,
    ) -> None:
        meta = metadata or {}
        if meta.get("webui"):
            user_obj: dict[str, Any] = {
                "event": "user",
                "chat_id": chat_id,
                "text": content,
            }
            if media:
                user_obj["media_paths"] = list(media)
            review_target = str(meta.get("review_target") or "").strip()
            review_target_type = str(meta.get("review_target_type") or "").strip()
            review_mode_variant = str(meta.get("review_mode_variant") or "").strip()
            review_action = str(meta.get("review_action") or "").strip()
            if review_target:
                user_obj["review_target"] = review_target
            if review_target_type:
                user_obj["review_target_type"] = review_target_type
            if review_mode_variant:
                user_obj["review_mode_variant"] = review_mode_variant
            if review_action:
                user_obj["review_action"] = review_action
            if isinstance(meta.get("review_focus"), list):
                user_obj["review_focus"] = meta["review_focus"]
            self._try_append_webui_transcript(chat_id, user_obj)
        await super()._handle_message(
            sender_id,
            chat_id,
            content,
            media,
            metadata,
            session_key,
            is_dm,
        )

    def _augment_media_urls(self, payload: dict[str, Any]) -> None:
        """Mutate *payload* in place: each message's ``media`` path list is
        replaced by a parallel ``media_urls`` list of signed fetch URLs.

        Messages without media or with non-string path entries are left
        untouched. Paths that no longer live inside ``media_dir`` (e.g. the
        file was deleted, or the dir was relocated) are silently skipped;
        the client falls back to the historical-replay placeholder tile.
        """
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            media = msg.get("media")
            if not isinstance(media, list) or not media:
                continue
            urls: list[dict[str, str]] = []
            for entry in media:
                if not isinstance(entry, str) or not entry:
                    continue
                signed = self._sign_media_path(Path(entry))
                if signed is None:
                    continue
                urls.append({"url": signed, "name": Path(entry).name})
            if urls:
                msg["media_urls"] = urls
            # Always drop the raw paths from the wire payload.
            msg.pop("media", None)

    def _sign_media_path(self, abs_path: Path) -> str | None:
        """Return a ``/api/media/<sig>/<payload>`` URL for *abs_path*, or
        ``None`` when the path does not resolve inside the media root.

        The URL is self-authenticating: the signature binds the payload to
        this process's ``_media_secret``, so only paths we chose to sign can
        be fetched. The returned path is relative to the server origin; the
        client joins it against this server's HTTP origin (same host as WS).
        """
        try:
            media_root = get_media_dir().resolve()
            rel = abs_path.resolve().relative_to(media_root)
        except (OSError, ValueError):
            return None
        payload = _b64url_encode(rel.as_posix().encode("utf-8"))
        mac = hmac.new(
            self._media_secret, payload.encode("ascii"), hashlib.sha256
        ).digest()[:16]
        return f"/api/media/{_b64url_encode(mac)}/{payload}"

    def _sign_or_stage_media_path(self, path: Path) -> dict[str, str] | None:
        """Return a signed media URL payload for *path*.

        Persisted inbound media already lives under ``get_media_dir`` and can
        be signed directly. Outbound bot-generated files may live anywhere on
        disk; copy those into the websocket media bucket first so the browser
        can fetch them through the existing signed media route without
        exposing arbitrary filesystem paths.
        """
        signed = self._sign_media_path(path)
        if signed is not None:
            return {"url": signed, "name": path.name}
        try:
            if not path.is_file():
                return None
            media_dir = get_media_dir("websocket")
            safe_name = safe_filename(path.name) or "attachment"
            staged = media_dir / f"{uuid.uuid4().hex[:12]}-{safe_name}"
            shutil.copyfile(path, staged)
        except OSError as exc:
            self.logger.warning("failed to stage outbound media {}: {}", path, exc)
            return None
        signed = self._sign_media_path(staged)
        if signed is None:
            return None
        return {"url": signed, "name": path.name}

    def _handle_media_fetch(self, sig: str, payload: str) -> Response:
        """Serve a single media file previously signed via
        :meth:`_sign_media_path`. Validates the signature, decodes the
        payload to a relative path, and streams the file bytes with a
        long-lived immutable cache header (the URL already encodes the
        file identity, so caches can be aggressive)."""
        try:
            provided_mac = _b64url_decode(sig)
        except (ValueError, binascii.Error):
            return _http_error(401, "invalid signature")
        expected_mac = hmac.new(
            self._media_secret, payload.encode("ascii"), hashlib.sha256
        ).digest()[:16]
        if not hmac.compare_digest(expected_mac, provided_mac):
            return _http_error(401, "invalid signature")
        try:
            rel_bytes = _b64url_decode(payload)
            rel_str = rel_bytes.decode("utf-8")
        except (ValueError, binascii.Error, UnicodeDecodeError):
            return _http_error(400, "invalid payload")
        # An attacker who somehow bypassed the HMAC check would still need
        # the resolved path to escape the media root; guard defensively.
        try:
            media_root = get_media_dir().resolve()
            candidate = (media_root / rel_str).resolve()
            candidate.relative_to(media_root)
        except (OSError, ValueError):
            return _http_error(404, "not found")
        if not candidate.is_file():
            return _http_error(404, "not found")
        try:
            body = candidate.read_bytes()
        except OSError:
            return _http_error(500, "read error")
        mime, _ = mimetypes.guess_type(candidate.name)
        if mime not in _MEDIA_ALLOWED_MIMES:
            mime = "application/octet-stream"
        return _http_response(
            body,
            content_type=mime,
            extra_headers=[
                ("Cache-Control", "private, max-age=31536000, immutable"),
                # Paired with the MIME whitelist above: prevents browsers from
                # MIME-sniffing an octet-stream fallback into executable HTML.
                ("X-Content-Type-Options", "nosniff"),
            ],
        )

    def _handle_session_delete(self, request: WsRequest, key: str) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self._session_manager is None:
            return _http_error(503, "session manager unavailable")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        # Same boundary as ``_handle_session_messages``: mutations apply only to
        # websocket-channel sessions; deletion unlinks local JSONL — keep scope narrow.
        if not self._is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        deleted = self._session_manager.delete_session(decoded_key)
        delete_webui_thread(decoded_key)
        return _http_json_response({"deleted": bool(deleted)})

    def _handle_session_update(self, request: WsRequest, key: str) -> Response:
        if not self._check_api_token(request):
            return _http_error(401, "Unauthorized")
        if self._session_manager is None:
            return _http_error(503, "session manager unavailable")
        decoded_key = _decode_api_key(key)
        if decoded_key is None:
            return _http_error(400, "invalid session key")
        if not self._is_websocket_channel_session_key(decoded_key):
            return _http_error(404, "session not found")
        # Read updates from query parameters (websockets HTTP only supports GET).
        query = request.path.split("?", 1)[1] if "?" in request.path else ""
        params = parse_qs(query)
        updates: dict[str, Any] = {}
        if "pinned" in params:
            val = params["pinned"][0].lower()
            if val not in ("true", "false"):
                return _http_error(400, "pinned must be 'true' or 'false'")
            updates["pinned"] = val == "true"
        if "custom_title" in params:
            title = params["custom_title"][0]
            if len(title) > 200:
                return _http_error(400, "custom_title too long")
            updates["custom_title"] = title
        if not updates:
            return _http_error(400, "no valid fields to update")
        updated = self._session_manager.update_session_metadata(decoded_key, updates)
        if updated is None:
            return _http_error(404, "session not found")
        return _http_json_response({"updated": True, "metadata": updated})

    def _serve_static(self, request_path: str) -> Response | None:
        """Resolve *request_path* against the built SPA directory; SPA fallback to index.html."""
        assert self._static_dist_path is not None
        rel = request_path.lstrip("/")
        if not rel:
            rel = "index.html"
        # Reject path-traversal attempts and absolute targets.
        if ".." in rel.split("/") or rel.startswith("/"):
            return _http_error(403, "Forbidden")
        candidate = (self._static_dist_path / rel).resolve()
        try:
            candidate.relative_to(self._static_dist_path)
        except ValueError:
            return _http_error(403, "Forbidden")
        if not candidate.is_file():
            # SPA history-mode fallback: unknown routes serve index.html so the
            # client-side router can render them.
            index = self._static_dist_path / "index.html"
            if index.is_file():
                candidate = index
            else:
                return None
        try:
            body = candidate.read_bytes()
        except OSError as e:
            self.logger.warning("static: failed to read {}: {}", candidate, e)
            return _http_error(500, "Internal Server Error")
        ctype, _ = mimetypes.guess_type(candidate.name)
        if ctype is None:
            ctype = "application/octet-stream"
        if ctype.startswith("text/") or ctype in {"application/javascript", "application/json"}:
            ctype = f"{ctype}; charset=utf-8"
        # Hash-named build assets are cache-friendly; index.html must stay fresh.
        if candidate.name == "index.html":
            cache = "no-cache"
        else:
            cache = "public, max-age=31536000, immutable"
        return _http_response(
            body,
            status=200,
            content_type=ctype,
            extra_headers=[("Cache-Control", cache)],
        )

    def _authorize_websocket_handshake(self, connection: Any, query: dict[str, list[str]]) -> Any:
        supplied = _query_first(query, "token")
        static_token = self.config.token.strip()

        if static_token:
            if supplied and hmac.compare_digest(supplied, static_token):
                return None
            if supplied and self._take_issued_token_if_valid(supplied):
                return None
            return connection.respond(401, "Unauthorized")

        if self.config.websocket_requires_token:
            if supplied and self._take_issued_token_if_valid(supplied):
                return None
            return connection.respond(401, "Unauthorized")

        if supplied:
            self._take_issued_token_if_valid(supplied)
        return None

    async def _aiohttp_bootstrap(self, request: web.Request) -> web.Response:
        secret = self.config.token_issue_secret.strip() or self.config.token.strip()
        if secret:
            if not _issue_route_secret_matches(request.headers, secret):
                return web.json_response({"error": "Unauthorized"}, status=401)
        elif request.remote not in _LOCALHOSTS:
            return web.json_response({"error": "bootstrap is localhost-only"}, status=403)
        payload = self._issue_bootstrap_token_payload()
        if payload is None:
            return web.json_response({"error": "too many outstanding tokens"}, status=429)
        return web.json_response(payload)

    async def _aiohttp_token_issue(self, request: web.Request) -> web.Response:
        secret = self.config.token_issue_secret.strip()
        if secret and not _issue_route_secret_matches(request.headers, secret):
            return web.json_response({"error": "Unauthorized"}, status=401)
        payload = self._issue_bootstrap_token_payload()
        if payload is None:
            return web.json_response({"error": "too many outstanding tokens"}, status=429)
        return web.json_response({"token": payload["token"], "expires_in": payload["expires_in"]})

    async def _aiohttp_sessions(self, request: web.Request) -> web.Response:
        if not self._check_aiohttp_api_token(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            return web.json_response(self._webui_sessions_payload())
        except RuntimeError as exc:
            return web.json_response({"error": str(exc)}, status=503)

    async def _aiohttp_session_messages(self, request: web.Request) -> web.Response:
        if not self._check_aiohttp_api_token(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        key = _decode_api_key(request.match_info["key"])
        if key is None:
            return web.json_response({"error": "invalid session key"}, status=400)
        try:
            payload = self._session_messages_payload(key)
        except RuntimeError as exc:
            return web.json_response({"error": str(exc)}, status=503)
        if payload is None:
            return web.json_response({"error": "session not found"}, status=404)
        return web.json_response(payload)

    async def _aiohttp_webui_thread(self, request: web.Request) -> web.Response:
        if not self._check_aiohttp_api_token(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        key = _decode_api_key(request.match_info["key"])
        if key is None:
            return web.json_response({"error": "invalid session key"}, status=400)
        payload = self._webui_thread_payload(key)
        if payload is None:
            return web.json_response({"error": "webui thread not found"}, status=404)
        return web.json_response(payload)

    async def _aiohttp_code_context(self, request: web.Request) -> web.Response:
        if not self._check_aiohttp_api_token(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        key = _decode_api_key(request.match_info["key"])
        if key is None:
            return web.json_response({"error": "invalid session key"}, status=400)
        try:
            payload = self._code_context_payload(
                key,
                file_path=request.query.get("file", ""),
                line=self._int_query_value(request.query.get("line"), 1, min_value=1, max_value=10_000_000),
                before=self._int_query_value(
                    request.query.get("before"),
                    _CODE_CONTEXT_DEFAULT_BEFORE,
                    min_value=0,
                    max_value=_CODE_CONTEXT_MAX_WINDOW,
                ),
                after=self._int_query_value(
                    request.query.get("after"),
                    _CODE_CONTEXT_DEFAULT_AFTER,
                    min_value=0,
                    max_value=_CODE_CONTEXT_MAX_WINDOW,
                ),
            )
        except CodeContextError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        return web.json_response(payload)

    async def _aiohttp_session_delete(self, request: web.Request) -> web.Response:
        if not self._check_aiohttp_api_token(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        key = _decode_api_key(request.match_info["key"])
        if key is None:
            return web.json_response({"error": "invalid session key"}, status=400)
        try:
            deleted = self._delete_session_key(key)
        except RuntimeError as exc:
            return web.json_response({"error": str(exc)}, status=503)
        if deleted is None:
            return web.json_response({"error": "session not found"}, status=404)
        return web.json_response({"deleted": bool(deleted)})

    async def _aiohttp_session_update(self, request: web.Request) -> web.Response:
        if not self._check_aiohttp_api_token(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        if self._session_manager is None:
            return web.json_response({"error": "session manager unavailable"}, status=503)
        key = _decode_api_key(request.match_info["key"])
        if key is None:
            return web.json_response({"error": "invalid session key"}, status=400)
        if not self._is_websocket_channel_session_key(key):
            return web.json_response({"error": "session not found"}, status=404)
        updates: dict[str, Any] = {}
        pinned = request.query.get("pinned")
        if pinned is not None:
            val = pinned.lower()
            if val not in ("true", "false"):
                return web.json_response({"error": "pinned must be 'true' or 'false'"}, status=400)
            updates["pinned"] = val == "true"
        custom_title = request.query.get("custom_title")
        if custom_title is not None:
            if len(custom_title) > 200:
                return web.json_response({"error": "custom_title too long"}, status=400)
            updates["custom_title"] = custom_title
        if not updates:
            return web.json_response({"error": "no valid fields to update"}, status=400)
        updated = self._session_manager.update_session_metadata(key, updates)
        if updated is None:
            return web.json_response({"error": "session not found"}, status=404)
        return web.json_response({"updated": True, "metadata": updated})

    async def _aiohttp_media_fetch(self, request: web.Request) -> web.Response:
        sig = request.match_info["sig"]
        payload = request.match_info["payload"]
        try:
            provided_mac = _b64url_decode(sig)
        except (ValueError, binascii.Error):
            return web.Response(status=401, text="invalid signature")
        expected_mac = hmac.new(
            self._media_secret, payload.encode("ascii"), hashlib.sha256
        ).digest()[:16]
        if not hmac.compare_digest(expected_mac, provided_mac):
            return web.Response(status=401, text="invalid signature")
        try:
            rel_bytes = _b64url_decode(payload)
            rel_str = rel_bytes.decode("utf-8")
        except (ValueError, binascii.Error, UnicodeDecodeError):
            return web.Response(status=400, text="invalid payload")
        try:
            media_root = get_media_dir().resolve()
            candidate = (media_root / rel_str).resolve()
            candidate.relative_to(media_root)
        except (OSError, ValueError):
            return web.Response(status=404, text="not found")
        if not candidate.is_file():
            return web.Response(status=404, text="not found")
        try:
            body = candidate.read_bytes()
        except OSError:
            return web.Response(status=500, text="read error")
        mime, _ = mimetypes.guess_type(candidate.name)
        if mime not in _MEDIA_ALLOWED_MIMES:
            mime = "application/octet-stream"
        return web.Response(
            body=body,
            content_type=mime,
            headers={
                "Cache-Control": "private, max-age=31536000, immutable",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def _aiohttp_ws_handler(self, request: web.Request) -> web.StreamResponse:
        if not _is_aiohttp_websocket_upgrade(request):
            if self._static_dist_path is not None:
                return await self._aiohttp_static(request)
            return web.Response(status=404, text="Not Found")

        supplied = request.query.get("token")
        static_token = self.config.token.strip()
        authorized = False
        if static_token:
            authorized = bool(supplied and hmac.compare_digest(supplied, static_token))
            if not authorized:
                authorized = self._take_issued_token_if_valid(supplied)
        elif self.config.websocket_requires_token:
            authorized = self._take_issued_token_if_valid(supplied)
        else:
            authorized = True
            if supplied:
                self._take_issued_token_if_valid(supplied)
        client_id = request.query.get("client_id", "")
        if not self.is_allowed(client_id):
            return web.Response(status=403, text="Forbidden")
        if not authorized:
            return web.Response(status=401, text="Unauthorized")

        ws = web.WebSocketResponse(max_msg_size=self.config.max_message_bytes)
        await ws.prepare(request)
        conn = _AiohttpConnection(request, ws)
        await self._connection_loop_aiohttp(conn)
        return ws

    async def _connection_loop_aiohttp(self, connection: _AiohttpConnection) -> None:
        request = connection.request
        client_id = (request.query.get("client_id") or "").strip()
        if not client_id:
            client_id = f"anon-{uuid.uuid4().hex[:12]}"
        elif len(client_id) > 128:
            self.logger.warning("client_id too long ({} chars), truncating", len(client_id))
            client_id = client_id[:128]

        default_chat_id = str(uuid.uuid4())
        try:
            await connection.send(
                json.dumps(
                    {"event": "ready", "chat_id": default_chat_id, "client_id": client_id},
                    ensure_ascii=False,
                )
            )
            self._conn_default[connection] = default_chat_id
            self._attach(connection, default_chat_id)
            await self._hydrate_after_subscribe(default_chat_id)

            async for msg in connection.ws:
                if msg.type == web.WSMsgType.TEXT:
                    raw = msg.data
                elif msg.type == web.WSMsgType.BINARY:
                    try:
                        raw = msg.data.decode("utf-8")
                    except UnicodeDecodeError:
                        self.logger.warning("ignoring non-utf8 binary frame")
                        continue
                elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.CLOSED, web.WSMsgType.ERROR):
                    break
                else:
                    continue
                envelope = _parse_envelope(raw)
                if envelope is not None:
                    await self._dispatch_envelope(connection, client_id, envelope)
                    continue
                content = _parse_inbound_payload(raw)
                if content is None:
                    continue
                await self._handle_message(
                    sender_id=client_id,
                    chat_id=default_chat_id,
                    content=content,
                    metadata={"remote": connection.remote_address},
                    is_dm=False,
                )
        except Exception as exc:
            self.logger.debug("connection ended: {}", exc)
        finally:
            await self._cleanup_connection(connection)

    def _build_aiohttp_app(self) -> web.Application:
        app = web.Application(client_max_size=max(self.config.max_message_bytes, 32 * 1024 * 1024))
        if self.config.token_issue_path:
            app.router.add_get(_normalize_config_path(self.config.token_issue_path), self._aiohttp_token_issue)
        app.router.add_get("/webui/bootstrap", self._aiohttp_bootstrap)
        app.router.add_get("/api/sessions", self._aiohttp_sessions)
        app.router.add_get("/api/sessions/{key}/messages", self._aiohttp_session_messages)
        app.router.add_get("/api/sessions/{key}/webui-thread", self._aiohttp_webui_thread)
        app.router.add_get("/api/sessions/{key}/code-context", self._aiohttp_code_context)
        app.router.add_post("/api/sessions/{key}/delete", self._aiohttp_session_delete)
        app.router.add_get("/api/sessions/{key}/delete", self._aiohttp_session_delete)
        app.router.add_get("/api/sessions/{key}/update", self._aiohttp_session_update)
        app.router.add_get("/api/media/{sig}/{payload}", self._aiohttp_media_fetch)
        app.router.add_get(self._expected_path(), self._aiohttp_ws_handler)
        if self._static_dist_path is not None:
            app.router.add_get("/{tail:.*}", self._aiohttp_static)
        return app

    async def _aiohttp_static(self, request: web.Request) -> web.Response:
        if self._static_dist_path is None:
            return web.Response(status=404, text="Not Found")
        rel = request.match_info.get("tail", "").lstrip("/") or "index.html"
        if ".." in rel.split("/") or rel.startswith("/"):
            return web.Response(status=403, text="Forbidden")
        candidate = (self._static_dist_path / rel).resolve()
        try:
            candidate.relative_to(self._static_dist_path)
        except ValueError:
            return web.Response(status=403, text="Forbidden")
        if not candidate.is_file():
            index = self._static_dist_path / "index.html"
            if not index.is_file():
                return web.Response(status=404, text="Not Found")
            candidate = index
        try:
            body = candidate.read_bytes()
        except OSError:
            return web.Response(status=500, text="Internal Server Error")
        ctype, _ = mimetypes.guess_type(candidate.name)
        if ctype is None:
            ctype = "application/octet-stream"
        charset = (
            "utf-8"
            if ctype.startswith("text/") or ctype in {"application/javascript", "application/json"}
            else None
        )
        cache = "no-cache" if candidate.name == "index.html" else "public, max-age=31536000, immutable"
        return web.Response(body=body, content_type=ctype, charset=charset, headers={"Cache-Control": cache})

    async def start(self) -> None:
        self._running = True
        self._stop_event = asyncio.Event()

        ssl_context = self._build_ssl_context()
        scheme = "https" if ssl_context else "http"
        ws_scheme = "wss" if ssl_context else "ws"

        self.logger.info(
            "WebUI gateway listening on {}://{}:{}",
            scheme,
            self.config.host,
            self.config.port,
        )
        self.logger.info(
            "WebSocket route enabled at {}://{}:{}{}",
            ws_scheme,
            self.config.host,
            self.config.port,
            self.config.path,
        )
        if self.config.token_issue_path:
            self.logger.info(
                "WebSocket token issue route: {}://{}:{}{}",
                scheme,
                self.config.host,
                self.config.port,
                _normalize_config_path(self.config.token_issue_path),
            )

        async def runner() -> None:
            app = self._build_aiohttp_app()
            self._aiohttp_runner = web.AppRunner(app)
            await self._aiohttp_runner.setup()
            site = web.TCPSite(
                self._aiohttp_runner,
                self.config.host,
                self.config.port,
                ssl_context=ssl_context,
            )
            await site.start()
            assert self._stop_event is not None
            await self._stop_event.wait()
            await self._aiohttp_runner.cleanup()
            self._aiohttp_runner = None

        self._server_task = asyncio.create_task(runner())
        await self._server_task

    async def _connection_loop(self, connection: Any) -> None:
        request = connection.request
        path_part = request.path if request else "/"
        _, query = _parse_request_path(path_part)
        client_id_raw = _query_first(query, "client_id")
        client_id = client_id_raw.strip() if client_id_raw else ""
        if not client_id:
            client_id = f"anon-{uuid.uuid4().hex[:12]}"
        elif len(client_id) > 128:
            self.logger.warning("client_id too long ({} chars), truncating", len(client_id))
            client_id = client_id[:128]

        default_chat_id = str(uuid.uuid4())

        try:
            await connection.send(
                json.dumps(
                    {
                        "event": "ready",
                        "chat_id": default_chat_id,
                        "client_id": client_id,
                    },
                    ensure_ascii=False,
                )
            )
            # Register only after ready is successfully sent to avoid out-of-order sends
            self._conn_default[connection] = default_chat_id
            self._attach(connection, default_chat_id)
            await self._hydrate_after_subscribe(default_chat_id)

            async for raw in connection:
                if isinstance(raw, bytes):
                    try:
                        raw = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        self.logger.warning("ignoring non-utf8 binary frame")
                        continue

                envelope = _parse_envelope(raw)
                if envelope is not None:
                    await self._dispatch_envelope(connection, client_id, envelope)
                    continue

                content = _parse_inbound_payload(raw)
                if content is None:
                    continue
                # WebSocket already authenticates at handshake time (token),
                # so pairing is not applicable. Treat as non-DM to avoid
                # sending pairing codes to an already-authenticated client.
                await self._handle_message(
                    sender_id=client_id,
                    chat_id=default_chat_id,
                    content=content,
                    metadata={"remote": getattr(connection, "remote_address", None)},
                    is_dm=False,
                )
        except Exception as e:
            self.logger.debug("connection ended: {}", e)
        finally:
            await self._cleanup_connection(connection)

    def _save_envelope_media(
        self,
        media: list[Any],
    ) -> tuple[list[str], str | None]:
        """Decode and persist ``media`` items from a ``message`` envelope.

        Returns ``(paths, None)`` on success or ``([], reason)`` on the first
        failure — the caller is expected to surface ``reason`` to the client
        and skip publishing so no half-formed message ever reaches the agent.
        On failure, any files already written to disk earlier in the same
        call are unlinked so partial ingress doesn't leak orphan files.
        ``reason`` is a short, stable token suitable for UI localization.

        Shape: ``list[{"data_url": str, "name"?: str | None}]``.
        """
        image_count = 0
        video_count = 0
        for item in media:
            mime = _extract_data_url_mime(item.get("data_url", "")) if isinstance(item, dict) else None
            if mime in _VIDEO_MIME_ALLOWED:
                video_count += 1
            elif mime in _IMAGE_MIME_ALLOWED:
                image_count += 1
        if image_count > _MAX_IMAGES_PER_MESSAGE:
            return [], "too_many_images"
        if video_count > _MAX_VIDEOS_PER_MESSAGE:
            return [], "too_many_videos"

        media_dir = get_media_dir("websocket")
        paths: list[str] = []

        def _abort(reason: str) -> tuple[list[str], str]:
            for p in paths:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError as exc:
                    self.logger.warning(
                        "failed to unlink partial media {}: {}", p, exc
                    )
            return [], reason

        for item in media:
            if not isinstance(item, dict):
                return _abort("malformed")
            data_url = item.get("data_url")
            if not isinstance(data_url, str) or not data_url:
                return _abort("malformed")
            mime = _extract_data_url_mime(data_url)
            if mime is None:
                return _abort("decode")
            if mime not in _UPLOAD_MIME_ALLOWED:
                return _abort("mime")
            is_video = mime in _VIDEO_MIME_ALLOWED
            max_bytes = _MAX_VIDEO_BYTES if is_video else _MAX_IMAGE_BYTES
            try:
                saved = save_base64_data_url(
                    data_url, media_dir, max_bytes=max_bytes,
                )
            except FileSizeExceeded:
                return _abort("size")
            except Exception as exc:
                self.logger.warning("media decode failed: {}", exc)
                return _abort("decode")
            if saved is None:
                return _abort("decode")
            paths.append(saved)
        return paths, None

    async def _dispatch_envelope(
        self,
        connection: Any,
        client_id: str,
        envelope: dict[str, Any],
    ) -> None:
        """Route one typed inbound envelope (``new_chat`` / ``attach`` / ``message``)."""
        t = envelope.get("type")
        if t == "new_chat":
            new_id = str(uuid.uuid4())
            self._attach(connection, new_id)
            await self._send_event(connection, "attached", chat_id=new_id)
            await self._hydrate_after_subscribe(new_id)
            return
        if t == "attach":
            cid = envelope.get("chat_id")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            self._attach(connection, cid)
            await self._send_event(connection, "attached", chat_id=cid)
            await self._hydrate_after_subscribe(cid)
            return
        if t == "message":
            cid = envelope.get("chat_id")
            content = envelope.get("content")
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            if not isinstance(content, str):
                await self._send_event(connection, "error", detail="missing content")
                return

            raw_media = envelope.get("media")
            media_paths: list[str] = []
            if raw_media is not None:
                if not isinstance(raw_media, list):
                    await self._send_event(
                        connection, "error",
                        detail="image_rejected", reason="malformed",
                    )
                    return
                media_paths, reason = self._save_envelope_media(raw_media)
                if reason is not None:
                    await self._send_event(
                        connection, "error",
                        detail="image_rejected", reason=reason,
                    )
                    return

            raw_review_target = envelope.get("review_target")
            raw_review_target_type = envelope.get("review_target_type")
            raw_review_mode_variant = envelope.get("review_mode_variant")
            raw_review_action = envelope.get("review_action")
            raw_review_focus = envelope.get("review_focus")
            has_review_payload = (
                isinstance(raw_review_target, str)
                or isinstance(raw_review_target_type, str)
                or isinstance(raw_review_mode_variant, str)
                or isinstance(raw_review_action, str)
                or isinstance(raw_review_focus, list)
            )
            normalized_review_action: str | None = None
            if isinstance(raw_review_action, str):
                try:
                    normalized_review_action = normalize_review_action(raw_review_action).value
                except ValueError as exc:
                    await self._send_event(connection, "error", detail=str(exc))
                    return

            # Allow image-only and review-only turns.
            if not content.strip() and not media_paths and not has_review_payload:
                await self._send_event(connection, "error", detail="missing content")
                return

            # Auto-attach on first use so clients can one-shot without a separate attach.
            self._attach(connection, cid)
            await self._hydrate_after_subscribe(cid)
            if (
                self._session_manager is not None
                and has_review_payload
            ):
                session_key = f"websocket:{cid}"
                session = self._session_manager.get_or_create(session_key)
                session.metadata["review_mode"] = True
                if isinstance(raw_review_mode_variant, str):
                    mode = raw_review_mode_variant.strip().lower()
                    if mode in {"quick", "full", "deep"}:
                        session.metadata["review_mode_variant"] = mode
                    else:
                        session.metadata.pop("review_mode_variant", None)
                if isinstance(raw_review_target, str):
                    target = raw_review_target.strip()
                    if target:
                        session.metadata["review_target"] = target
                    else:
                        session.metadata.pop("review_target", None)
                if isinstance(raw_review_action, str):
                    session.metadata["review_action"] = normalized_review_action
                if isinstance(raw_review_focus, list):
                    focus = [str(item).strip() for item in raw_review_focus if str(item).strip()]
                    if focus:
                        session.metadata["review_focus"] = focus
                    else:
                        session.metadata.pop("review_focus", None)
                target_type = normalize_review_target_type(
                    raw_review_target_type if isinstance(raw_review_target_type, str) else None,
                    session.metadata.get("review_target"),
                )
                if target_type:
                    session.metadata["review_target_type"] = target_type
                else:
                    session.metadata.pop("review_target_type", None)
                self._session_manager.save(session)
            metadata: dict[str, Any] = {"remote": getattr(connection, "remote_address", None)}
            if envelope.get("webui") is True:
                metadata["webui"] = True
            if isinstance(raw_review_target, str):
                metadata["review_target"] = raw_review_target.strip()
            if isinstance(raw_review_target_type, str):
                metadata["review_target_type"] = raw_review_target_type
            if isinstance(raw_review_mode_variant, str):
                metadata["review_mode_variant"] = raw_review_mode_variant
            if isinstance(raw_review_action, str):
                metadata["review_action"] = normalized_review_action
            if isinstance(raw_review_focus, list):
                metadata["review_focus"] = [str(item).strip() for item in raw_review_focus if str(item).strip()]
            if has_review_payload:
                logger.info(
                    "ws.review.request cid={} webui={} target_type={} mode={} action={} focus_count={} content_chars={} media_count={}",
                    cid,
                    envelope.get("webui") is True,
                    raw_review_target_type if isinstance(raw_review_target_type, str) else "",
                    raw_review_mode_variant if isinstance(raw_review_mode_variant, str) else "",
                    normalized_review_action or "",
                    len(metadata.get("review_focus", [])) if isinstance(metadata.get("review_focus"), list) else 0,
                    len(content),
                    len(media_paths),
                )
            # Normalize empty text for review-only turns so the model receives
            # an explicit user intent rather than inheriting the previous turn.
            if has_review_payload and not content.strip():
                content = "审查"
                logger.info(
                    "ws.review_default_prompt cid={} action={} focus_count={}",
                    cid,
                    raw_review_action,
                    len(raw_review_focus) if isinstance(raw_review_focus, list) else 0,
                )
            await self._handle_message(
                sender_id=client_id,
                chat_id=cid,
                content=content,
                media=media_paths or None,
                metadata=metadata,
                is_dm=False,
            )
            return
        if t == "permission_response":
            request_id = envelope.get("request_id")
            approved = envelope.get("approved", False)
            cid = envelope.get("chat_id")
            if not isinstance(request_id, str) or not request_id:
                await self._send_event(connection, "error", detail="missing request_id")
                return
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            self._try_append_webui_transcript(
                cid, {"event": "permission_response", "request_id": request_id, "approved": bool(approved)}
            )
            await self.bus.publish_inbound(
                InboundMessage(
                    channel="websocket",
                    chat_id=cid,
                    sender_id=client_id,
                    content="",
                    metadata={"_permission_response": {"request_id": request_id, "approved": bool(approved)}},
                )
            )
            return
        if t == "set_session_permission":
            cid = envelope.get("chat_id")
            approval_enabled = bool(envelope.get("approval_enabled", False))
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            logger.info("session websocket:{} tool approval: {}", cid, "enabled" if approval_enabled else "disabled")
            if self._session_manager is not None:
                session_key = f"websocket:{cid}"
                session = self._session_manager.get_or_create(session_key)
                perms = session.metadata.setdefault("permissions", {})
                perms["approval_enabled"] = approval_enabled
                self._session_manager.save(session)
            await self._send_event(
                connection,
                "session_permission_updated",
                chat_id=cid,
                approval_enabled=approval_enabled,
            )
            return
        if t == "set_review_mode":
            cid = envelope.get("chat_id")
            enabled = bool(envelope.get("enabled", False))
            if not _is_valid_chat_id(cid):
                await self._send_event(connection, "error", detail="invalid chat_id")
                return
            logger.info("session websocket:{} review mode: {}", cid, "enabled" if enabled else "disabled")
            review_payload: dict[str, Any] = {}
            if self._session_manager is not None:
                session_key = f"websocket:{cid}"
                session = self._session_manager.get_or_create(session_key)
                session.metadata["review_mode"] = enabled
                if enabled:
                    raw_target = envelope.get("target")
                    if isinstance(raw_target, str):
                        target = raw_target.strip()
                        if target:
                            session.metadata["review_target"] = target
                        else:
                            session.metadata.pop("review_target", None)
                    raw_target_type = envelope.get("target_type")
                    target_type = normalize_review_target_type(
                        raw_target_type if isinstance(raw_target_type, str) else None,
                        session.metadata.get("review_target"),
                    )
                    if target_type:
                        session.metadata["review_target_type"] = target_type
                    else:
                        session.metadata.pop("review_target_type", None)
                    review_payload = _review_mode_payload(session.metadata)
                else:
                    _clear_review_metadata(session.metadata)
                self._session_manager.save(session)
            await self._send_event(
                connection,
                "review_mode_updated",
                chat_id=cid,
                enabled=enabled,
                **review_payload,
            )
            return
        await self._send_event(connection, "error", detail=f"unknown type: {t!r}")

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._stop_event:
            self._stop_event.set()
        if self._server_task:
            try:
                await self._server_task
            except Exception as e:
                self.logger.warning("server task error during shutdown: {}", e)
            self._server_task = None
        self._subs.clear()
        self._conn_chats.clear()
        self._conn_default.clear()
        self._issued_tokens.clear()
        self._api_tokens.clear()

    async def _safe_send_to(self, connection: Any, raw: str, *, label: str = "") -> None:
        """Send a raw frame to one connection, cleaning up on ConnectionClosed."""
        try:
            await connection.send(raw)
        except ConnectionClosed:
            await self._cleanup_connection(connection)
            self.logger.warning("connection gone{}", label)
        except Exception:
            self.logger.exception("send failed{}", label)
            raise

    async def send(self, msg: OutboundMessage) -> None:
        if msg.metadata.get("_runtime_model_updated"):
            await self.send_runtime_model_updated(
                model_name=msg.metadata.get("model"),
                model_preset=msg.metadata.get("model_preset"),
            )
            return

        if msg.metadata.get("_permission_request"):
            payload = msg.metadata["_permission_request"]
            self._try_append_webui_transcript(msg.chat_id, {"event": "permission_request", **payload})
            conns = list(self._subs.get(msg.chat_id, ()))
            for conn in conns:
                await self._send_event(
                    conn,
                    "permission_request",
                    chat_id=msg.chat_id,
                    **payload,
                )
            return

        # Snapshot the subscriber set so ConnectionClosed cleanups mid-iteration are safe.
        conns = list(self._subs.get(msg.chat_id, ()))
        persist_without_subscribers = False
        if not conns:
            if (
                msg.metadata.get("_turn_end")
                or msg.metadata.get("_session_updated")
                or msg.metadata.get("_goal_status")
            ):
                self.logger.debug("no active subscribers for chat_id={}", msg.chat_id)
                return
            # Progress and regular message events fall through to persist transcript
            # even without active connections (browser refresh gap).
            persist_without_subscribers = True
            if not msg.metadata.get("_progress") and not msg.metadata.get("_tool_hint"):
                self.logger.warning(
                    "no active subscribers for chat_id={}, persisting transcript only",
                    msg.chat_id,
                )
        if msg.metadata.get("_goal_status"):
            status = msg.metadata.get("goal_status")
            if status in ("running", "idle"):
                started_raw = msg.metadata.get("started_at", msg.metadata.get("goal_started_at"))
                await self.send_goal_status(
                    msg.chat_id,
                    status,
                    started_at=float(started_raw) if isinstance(started_raw, int | float) else None,
                )
            return
        # Signal that the agent has fully finished processing the current turn.
        if msg.metadata.get("_turn_end"):
            lat = msg.metadata.get("latency_ms")
            lat_i = int(lat) if isinstance(lat, (int, float)) else None
            trace = msg.metadata.get("turn_trace")
            trace_items = trace if isinstance(trace, list) else None
            await self.send_turn_end(
                msg.chat_id,
                latency_ms=lat_i,
                turn_trace=trace_items,
            )
            return
        if msg.metadata.get("_session_updated"):
            await self.send_session_updated(msg.chat_id)
            return
        text = msg.content
        payload: dict[str, Any] = {
            "event": "message",
            "chat_id": msg.chat_id,
            "text": text,
        }
        if msg.media:
            payload["media"] = msg.media
            urls: list[dict[str, str]] = []
            for entry in msg.media:
                signed = self._sign_or_stage_media_path(Path(entry))
                if signed is not None:
                    urls.append(signed)
            if urls:
                payload["media_urls"] = urls
        if msg.reply_to:
            payload["reply_to"] = msg.reply_to
        lat = msg.metadata.get("latency_ms")
        if isinstance(lat, (int, float)):
            payload["latency_ms"] = int(lat)
        if msg.metadata.get("_tool_events"):
            payload["tool_events"] = msg.metadata["_tool_events"]
        agent_ui = msg.metadata.get(OUTBOUND_META_AGENT_UI)
        if agent_ui is not None:
            payload["agent_ui"] = agent_ui
        # Mark intermediate agent breadcrumbs (tool-call hints, generic
        # progress strings) so WS clients can render them as subordinate
        # trace rows rather than conversational replies.
        if msg.metadata.get("_tool_hint"):
            payload["kind"] = "tool_hint"
        elif msg.metadata.get("_progress"):
            payload["kind"] = "progress"
        self._try_append_webui_transcript(msg.chat_id, payload)
        if persist_without_subscribers:
            return
        raw = json.dumps(payload, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" ")

    async def send_reasoning_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Push one chunk of model reasoning. Mirrors ``send_delta`` shape so
        clients receive a stream that opens, updates in place, and closes —
        rendered above the active assistant bubble with a shimmer header
        until the matching ``reasoning_end`` arrives.
        """
        if not delta:
            return
        meta = metadata or {}
        body: dict[str, Any] = {
            "event": "reasoning_delta",
            "chat_id": chat_id,
            "text": delta,
        }
        stream_id = meta.get("_stream_id")
        if stream_id is not None:
            body["stream_id"] = stream_id
        subagent_id = meta.get("_subagent_id")
        if subagent_id is not None:
            body["subagent_id"] = subagent_id
        subagent_label = meta.get("_subagent_label")
        if subagent_label is not None:
            body["subagent_label"] = subagent_label
        self._try_append_webui_transcript(chat_id, body)
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" reasoning ")

    async def send_reasoning_end(
        self,
        chat_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Close the current reasoning stream segment for in-place renderers."""
        meta = metadata or {}
        body: dict[str, Any] = {
            "event": "reasoning_end",
            "chat_id": chat_id,
        }
        stream_id = meta.get("_stream_id")
        if stream_id is not None:
            body["stream_id"] = stream_id
        subagent_id = meta.get("_subagent_id")
        if subagent_id is not None:
            body["subagent_id"] = subagent_id
        subagent_label = meta.get("_subagent_label")
        if subagent_label is not None:
            body["subagent_label"] = subagent_label
        self._try_append_webui_transcript(chat_id, body)
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" reasoning_end ")

    async def send_delta(
        self,
        chat_id: str,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        conns = list(self._subs.get(chat_id, ()))
        meta = metadata or {}
        if meta.get("_stream_end"):
            body: dict[str, Any] = {"event": "stream_end", "chat_id": chat_id}
        else:
            body = {
                "event": "delta",
                "chat_id": chat_id,
                "text": delta,
            }
        if meta.get("_stream_id") is not None:
            body["stream_id"] = meta["_stream_id"]
        if meta.get("_stream_kind") is not None:
            body["kind"] = str(meta["_stream_kind"])
        subagent_id = meta.get("_subagent_id")
        if subagent_id is not None:
            body["subagent_id"] = subagent_id
        subagent_label = meta.get("_subagent_label")
        if subagent_label is not None:
            body["subagent_label"] = subagent_label
        self._try_append_webui_transcript(chat_id, body)
        if not conns:
            return
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" stream ")

    async def send_subagent_status(
        self,
        chat_id: str,
        subagent_id: str,
        label: str,
        status: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Push a subagent lifecycle event so the frontend can create or
        update a per-subagent status card.

        ``status`` is ``"running"`` when the subagent starts and
        ``"completed"`` or ``"error"`` when it finishes.
        """
        body: dict[str, Any] = {
            "event": "subagent_status",
            "chat_id": chat_id,
            "subagent_id": subagent_id,
            "label": label,
            "status": status,
        }
        self._try_append_webui_transcript(chat_id, body)
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" subagent_status ")

    async def send_turn_end(
        self,
        chat_id: str,
        latency_ms: int | None = None,
        *,
        turn_trace: list[dict[str, Any]] | None = None,
    ) -> None:
        """Signal that the agent has fully finished processing the current turn."""
        body: dict[str, Any] = {"event": "turn_end", "chat_id": chat_id}
        if latency_ms is not None:
            body["latency_ms"] = int(latency_ms)
        if turn_trace is not None:
            body["turn_trace"] = turn_trace
        self._try_append_webui_transcript(chat_id, body)
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" turn_end ")

    async def send_goal_status(
        self,
        chat_id: str,
        status: str,
        *,
        started_at: float | None = None,
    ) -> None:
        """Notify subscribed clients that a turn started or finished (wall-clock hint)."""
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body: dict[str, Any] = {
            "event": "goal_status",
            "chat_id": chat_id,
            "status": status,
        }
        if status == "running" and started_at is not None:
            body["started_at"] = started_at
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" goal_status ")

    async def send_session_updated(self, chat_id: str) -> None:
        """Notify clients that session metadata changed outside the main turn."""
        conns = list(self._subs.get(chat_id, ()))
        if not conns:
            return
        body: dict[str, Any] = {"event": "session_updated", "chat_id": chat_id}
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" session_updated ")

    async def send_runtime_model_updated(
        self,
        *,
        model_name: Any,
        model_preset: Any = None,
    ) -> None:
        """Broadcast runtime model changes to every open websocket connection."""
        conns = list(self._conn_chats)
        if not conns or not isinstance(model_name, str) or not model_name.strip():
            return
        body: dict[str, Any] = {
            "event": "runtime_model_updated",
            "model_name": model_name.strip(),
        }
        if isinstance(model_preset, str) and model_preset.strip():
            body["model_preset"] = model_preset.strip()
        raw = json.dumps(body, ensure_ascii=False)
        for connection in conns:
            await self._safe_send_to(connection, raw, label=" runtime_model_updated ")
