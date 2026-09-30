"""Request security: login, CSRF and Host checks, request-body limit, audit log.

`require_login` and `audit_changes` are HTTP middleware functions and
`BodyLimit` is ASGI middleware; `product.app` installs all three (see
docs/threat_model.md for the threat each one answers).
"""
from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import logging
import os
import secrets
import threading
import time
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from fastapi.responses import Response

from product.web.uploads import _max_upload_bytes

OPEN_PATHS = {"/health"}
INGEST_PATHS = {"/ingest/alerts", "/ingest/alertmanager"}


def _auth_settings() -> tuple[str, str] | None:
    """Login is on when APP_PASSWORD is set; APP_USER defaults to "oncall"."""
    password = os.environ.get("APP_PASSWORD")
    if not password:
        return None
    return os.environ.get("APP_USER", "oncall"), password


def _credentials_ok(header: str | None, user: str, password: str) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    given_user, _, given_password = decoded.partition(":")
    user_ok = secrets.compare_digest(given_user.encode(), user.encode())
    password_ok = secrets.compare_digest(given_password.encode(), password.encode())
    return user_ok and password_ok


_LOGIN_LOCK = threading.Lock()
_LOGIN_FAILS: dict[str, list[float]] = {}
_LOGIN_UNTIL: dict[str, float] = {}


def _login_limits() -> tuple[int, float]:
    """Failed attempts inside the window, then a lock of the same length in seconds."""
    try:
        fails = int(os.environ.get("LOGIN_MAX_FAILS", "5"))
    except ValueError:
        fails = 5
    try:
        seconds = float(os.environ.get("LOGIN_LOCK_SECONDS", "60"))
    except ValueError:
        seconds = 60.0
    return max(1, fails), max(1.0, seconds)


def reset_login_attempts() -> None:
    """Clear the in-memory counters. Tests use this; the process otherwise keeps them."""
    with _LOGIN_LOCK:
        _LOGIN_FAILS.clear()
        _LOGIN_UNTIL.clear()


def _login_wait(client: str) -> float:
    """Seconds left on this client's lock, or 0."""
    with _LOGIN_LOCK:
        until = _LOGIN_UNTIL.get(client, 0.0)
        wait = until - time.monotonic()
        if wait <= 0:
            _LOGIN_UNTIL.pop(client, None)
            return 0.0
        return wait


def _note_login_failure(client: str) -> float:
    """Record one bad password. Returns the new lock length when this attempt starts one."""
    fails, seconds = _login_limits()
    now = time.monotonic()
    with _LOGIN_LOCK:
        recent = [stamp for stamp in _LOGIN_FAILS.get(client, []) if now - stamp < seconds]
        recent.append(now)
        if len(recent) >= fails:
            _LOGIN_FAILS.pop(client, None)
            _LOGIN_UNTIL[client] = now + seconds
            return seconds
        _LOGIN_FAILS[client] = recent
        return 0.0


def _note_login_success(client: str) -> None:
    with _LOGIN_LOCK:
        _LOGIN_FAILS.pop(client, None)
        _LOGIN_UNTIL.pop(client, None)


def _locked_response(wait: float) -> Response:
    return Response(
        "Too many login attempts",
        status_code=429,
        headers={"Retry-After": str(max(1, int(wait)))},
    )


def _ingest_token_ok(request: Request) -> bool | None:
    """None when INGEST_TOKEN is unset. Accepts only `Authorization: Bearer <token>`
    (Alertmanager `http_config.authorization`). A `?token=` query is refused
    because URLs end up in proxy and access logs."""
    token = os.environ.get("INGEST_TOKEN")
    if not token:
        return None
    header = request.headers.get("authorization") or ""
    given = header[7:].strip() if header.lower().startswith("bearer ") else ""
    return secrets.compare_digest(given.encode(), token.encode())


PROXY_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
LOCAL_HOSTNAMES = {"localhost", "127.0.0.1", "[::1]", "::1"}


def _host_name(value: str | None) -> str:
    """Host header or URL netloc without the port, lower case."""
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value.split("]", 1)[0] + "]"
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def _allowed_hosts() -> set[str]:
    extra = {h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()}
    return LOCAL_HOSTNAMES | extra


def _cross_site(request: Request) -> bool:
    """A browser form or fetch from another site (CSRF). Browsers send Origin on
    cross-site POSTs; Basic-auth credentials would be attached automatically."""
    origin = request.headers.get("origin")
    if not origin:
        referer = request.headers.get("referer")
        if not referer:
            return False  # curl, Alertmanager, test client: not a browser
        origin = referer
    if origin == "null":
        return True
    netloc = urlparse(origin).netloc
    return _host_name(netloc) != _host_name(request.headers.get("host"))


def _is_private(host: str | None) -> bool:
    """Loopback, private (RFC 1918, Docker bridge) and link-local count as private.
    A host that is not an IP address (test client, Unix socket) counts as local."""
    if not host:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return ip.is_loopback or ip.is_private or ip.is_link_local


def _is_loopback(host: str | None) -> bool:
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


async def require_login(request: Request, call_next):
    if request.method in UNSAFE_METHODS and _cross_site(request):
        return Response("Cross-site request refused", status_code=403)
    if request.url.path in INGEST_PATHS:
        ok = _ingest_token_ok(request)
        if ok is False:
            return Response("Bad ingest token", status_code=401)
        if ok is True:
            return await call_next(request)
    settings = _auth_settings()
    if settings is None:
        # No password: serve this machine and private networks only, unless the
        # operator says otherwise. A public address must never reach an open queue.
        # Behind a reverse proxy every client looks private, so a forwarded
        # request needs a password too. A loopback client must use a local Host
        # name, so a web page cannot reach the app through DNS rebinding.
        client = request.client.host if request.client else None
        open_access = os.environ.get("ALLOW_OPEN_ACCESS") == "1"
        if request.url.path not in OPEN_PATHS and not open_access:
            forwarded = any(request.headers.get(h) for h in PROXY_HEADERS)
            if forwarded or not _is_private(client):
                return Response(
                    "Set APP_PASSWORD before exposing this app (or ALLOW_OPEN_ACCESS=1).",
                    status_code=403,
                )
            if _is_loopback(client) and _host_name(request.headers.get("host")) not in _allowed_hosts():
                return Response("Unknown Host header (set ALLOWED_HOSTS)", status_code=403)
        return await call_next(request)
    if request.url.path in OPEN_PATHS:
        return await call_next(request)
    client = request.client.host if request.client else "-"
    wait = _login_wait(client)
    if wait:
        return _locked_response(wait)
    if not _credentials_ok(request.headers.get("authorization"), *settings):
        locked = _note_login_failure(client)
        if locked:
            return _locked_response(locked)
        return Response(
            "Login required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="On-call alert queue"'},
        )
    _note_login_success(client)
    return await call_next(request)


MULTIPART_PATHS = {"/jobs/upload", "/evaluate"}
UPLOAD_FILES = 3  # alerts + metrics + faults, each capped by _save_upload


class BodyTooLarge(HTTPException):
    def __init__(self, limit: int):
        super().__init__(413, f"Request body is larger than {limit // (1024 * 1024)} MB")


def _max_ingest_bytes() -> int:
    try:
        return int(float(os.environ.get("MAX_INGEST_MB", "10")) * 1024 * 1024)
    except ValueError:
        return 10 * 1024 * 1024


def _body_limit(path: str) -> int:
    if path in MULTIPART_PATHS:
        return UPLOAD_FILES * _max_upload_bytes() + 1024 * 1024  # form fields
    return _max_ingest_bytes()


class BodyLimit:
    """Stop a request body while it streams in, so a huge upload or ingest never
    reaches memory or temp disk in full. Content-Length is checked first; a
    chunked body is counted as it arrives."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = _body_limit(scope["path"])
        length = dict(scope.get("headers") or []).get(b"content-length", b"")
        if length.isdigit() and int(length) > limit:
            await self._refuse(send, limit)
            return
        seen = 0
        exceeded = False
        responded = False

        async def counted():
            nonlocal seen, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    # Stop reading here. The app sees a disconnect; whatever it
                    # answers is replaced by a 413 below.
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded(message):
            nonlocal responded
            if exceeded:
                if message["type"] == "http.response.start" and not responded:
                    responded = True
                    await self._refuse(send, limit)
                return
            if message["type"] == "http.response.start":
                responded = True
            await send(message)

        try:
            await self.app(scope, counted, guarded)
        except Exception:
            if not exceeded:
                raise
        if exceeded and not responded:
            await self._refuse(send, limit)

    @staticmethod
    async def _refuse(send, limit: int) -> None:
        body = json.dumps({"detail": BodyTooLarge(limit).detail}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})




AUDIT = logging.getLogger("oncall.audit")
if not AUDIT.handlers:  # uvicorn configures only its own loggers
    _audit_handler = logging.StreamHandler()
    _audit_handler.setFormatter(logging.Formatter("%(asctime)s audit %(message)s"))
    AUDIT.addHandler(_audit_handler)
    AUDIT.setLevel(logging.INFO)
AUDITED_PREFIXES = ("/ops/", "/settings", "/models")


def _login_user(header: str | None) -> str | None:
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        return base64.b64decode(header[6:].strip()).decode("utf-8").partition(":")[0] or None
    except (binascii.Error, UnicodeDecodeError):
        return None


async def audit_changes(request: Request, call_next):
    """Log who changed Live mode, keys, the Prometheus URL or settings, and the
    outcome (refused attempts too). Bodies are never logged: they hold secrets."""
    response = await call_next(request)
    if request.method in UNSAFE_METHODS and request.url.path.startswith(AUDITED_PREFIXES):
        AUDIT.info(
            "%s %s status=%s user=%s client=%s",
            request.method,
            request.url.path,
            response.status_code,
            _login_user(request.headers.get("authorization")) or "-",
            request.client.host if request.client else "-",
        )
    return response
