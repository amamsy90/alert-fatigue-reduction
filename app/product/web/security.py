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
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from product.web.uploads import _max_upload_bytes

OPEN_PATHS = {"/health"}
INGEST_PATHS = {"/ingest/alerts", "/ingest/alertmanager"}


def _auth_settings() -> tuple[str, str] | None:
    """Operator login is on when APP_PASSWORD is set; APP_USER defaults to "oncall"."""
    password = os.environ.get("APP_PASSWORD")
    if not password:
        return None
    return os.environ.get("APP_USER", "oncall"), password


def _viewer_settings() -> tuple[str, str] | None:
    """Read-only login. Unset means there is no viewer account."""
    password = os.environ.get("APP_VIEWER_PASSWORD")
    if not password:
        return None
    return os.environ.get("APP_VIEWER_USER", "viewer"), password


def _accounts() -> list[tuple[str, str, str]]:
    accounts = []
    operator = _auth_settings()
    if operator:
        accounts.append((operator[0], operator[1], "operator"))
    viewer = _viewer_settings()
    if viewer:
        accounts.append((viewer[0], viewer[1], "viewer"))
    return accounts


def _match_password(user: str, password: str) -> tuple[str, str] | None:
    for account_user, account_password, role in _accounts():
        user_ok = secrets.compare_digest(user.encode(), account_user.encode())
        password_ok = secrets.compare_digest(password.encode(), account_password.encode())
        if user_ok and password_ok:
            return account_user, role
    return None


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


_SESSION_LOCK = threading.Lock()
_SESSIONS: dict[str, tuple[str, str, float]] = {}
SESSION_SECONDS = 12 * 60 * 60
MAX_SESSIONS = 200
SESSION_COOKIE = "oncall_session"
_ROLE_FREE_POSTS = {"/login", "/logout"}


_INGEST_LOCK = threading.Lock()
_INGEST_HITS: dict[str, list[float]] = {}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def reset_login_attempts() -> None:
    """Clear the in-memory counters and sessions. Tests use this; the process otherwise keeps them."""
    with _LOGIN_LOCK:
        _LOGIN_FAILS.clear()
        _LOGIN_UNTIL.clear()
    with _SESSION_LOCK:
        _SESSIONS.clear()
    with _INGEST_LOCK:
        _INGEST_HITS.clear()


def ingest_alert_cap() -> int:
    """Largest number of alerts in one ingest POST (413 when exceeded)."""
    return max(1, _int_env("INGEST_MAX_ALERTS", 400))


def _ingest_batch_limits() -> tuple[int, float]:
    return max(1, _int_env("INGEST_MAX_BATCHES", 60)), max(1.0, _float_env("INGEST_WINDOW_SECONDS", 60.0))


def _note_ingest_batch(client: str) -> float:
    """Record one ingest POST. Returns the window length when this client is over the cap."""
    n, window = _ingest_batch_limits()
    now = time.monotonic()
    with _INGEST_LOCK:
        recent = [stamp for stamp in _INGEST_HITS.get(client, []) if now - stamp < window]
        if len(recent) >= n:
            _INGEST_HITS[client] = recent
            return window
        recent.append(now)
        _INGEST_HITS[client] = recent
        return 0.0


def _ingest_rate_response(request: Request) -> Response | None:
    wait = _note_ingest_batch(login_client_id(request))
    if not wait:
        return None
    return Response(
        "Too many ingest batches",
        status_code=429,
        headers={"Retry-After": str(max(1, int(wait)))},
    )


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


def _trusted_proxy_networks():
    nets = []
    for raw in os.environ.get("TRUSTED_PROXIES", "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            nets.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            continue
    return nets


def _is_trusted_proxy(host: str | None) -> bool:
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    return any(ip in net for net in _trusted_proxy_networks())


def _strip_host(value: str) -> str:
    """Bare IP from an X-Forwarded-For or Forwarded `for=` value (quotes,
    IPv6 brackets and a port removed)."""
    value = value.strip().strip('"')
    if value.startswith("["):
        return value[1:].split("]", 1)[0]
    if value.count(":") == 1:
        # IPv4 with a port; a bare IPv6 address has several colons.
        return value.split(":", 1)[0]
    return value


def _rightmost_untrusted(hops: list[str]) -> str | None:
    """Walk the proxy chain from the right and return the first hop that is
    not a trusted proxy.

    Proxies append the address they saw on the right, so everything to the
    left of the last trusted hop was written by the client and can be forged.
    Taking the leftmost value let a client rotate it and dodge the login lock.
    """
    hops = [h for h in (_strip_host(x) for x in hops) if h]
    for hop in reversed(hops):
        if not _is_trusted_proxy(hop):
            return hop
    # Every hop is a trusted proxy: the leftmost one is the closest we have.
    return hops[0] if hops else None


def _all_lines(request: Request, name: str) -> str:
    """Every line of a header, joined in arrival order.

    Some proxies add a new header line instead of appending to the client's
    one. Reading only the first line would return the value the client wrote,
    so the hops are read across all lines, as one comma-separated list.
    """
    return ",".join(request.headers.getlist(name))


def _forwarded_client(request: Request) -> str | None:
    xff = _all_lines(request, "x-forwarded-for")
    if xff:
        return _rightmost_untrusted(xff.split(","))
    forwarded = _all_lines(request, "forwarded")
    if forwarded:
        hops = []
        for item in forwarded.split(","):
            for part in item.split(";"):
                key, _, value = part.strip().partition("=")
                if key.lower() == "for" and value:
                    hops.append(value)
        found = _rightmost_untrusted(hops)
        if found:
            return found
    real = (request.headers.get("x-real-ip") or "").strip()
    return real or None


def login_client_id(request: Request) -> str:
    """Socket IP, or the forwarded client when that socket is in TRUSTED_PROXIES.

    The forwarded client is the rightmost X-Forwarded-For hop that is not itself
    a trusted proxy, so a value the client prepends is never used. Without
    TRUSTED_PROXIES, X-Forwarded-For is ignored so a client cannot split the
    login lock by spoofing the header.
    """
    host = request.client.host if request.client else "-"
    if _is_trusted_proxy(host):
        return _forwarded_client(request) or host
    return host or "-"


def _cookie_token(header: str | None) -> str | None:
    if not header:
        return None
    for part in header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == SESSION_COOKIE and value:
            return value
    return None


def _session_identity(cookie_header: str | None) -> tuple[str, str] | None:
    token = _cookie_token(cookie_header)
    if not token:
        return None
    with _SESSION_LOCK:
        entry = _SESSIONS.get(token)
        if entry is None:
            return None
        if entry[2] <= time.monotonic():
            _SESSIONS.pop(token, None)
            return None
        return entry[0], entry[1]


def _basic_identity(header: str | None) -> tuple[str, str] | None:
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    user, _, password = decoded.partition(":")
    return _match_password(user, password)


def _cookie_secure(request: Request) -> bool:
    if os.environ.get("COOKIE_SECURE") == "1":
        return True
    # Only a trusted proxy may tell us the original scheme.
    host = request.client.host if request.client else None
    if not _is_trusted_proxy(host):
        return False
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[-1].strip().lower()
    return forwarded == "https"


def _stamp_session(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/",
        max_age=SESSION_SECONDS,
    )


def _drop_session(cookie_header: str | None) -> None:
    token = _cookie_token(cookie_header)
    if not token:
        return
    with _SESSION_LOCK:
        _SESSIONS.pop(token, None)


async def require_login(request: Request, call_next):
    if request.method in UNSAFE_METHODS and _cross_site(request):
        return Response("Cross-site request refused", status_code=403)
    if request.url.path in INGEST_PATHS:
        ok = _ingest_token_ok(request)
        if ok is False:
            return Response("Bad ingest token", status_code=401)
        if ok is True:
            limited = _ingest_rate_response(request)
            return limited if limited is not None else await call_next(request)
    if not _accounts():
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
        if request.url.path in INGEST_PATHS:
            limited = _ingest_rate_response(request)
            if limited is not None:
                return limited
        return await call_next(request)
    if request.url.path in OPEN_PATHS or (request.url.path == "/login" and request.method == "POST"):
        return await call_next(request)
    client = login_client_id(request)
    wait = _login_wait(client)
    if wait:
        return _locked_response(wait)
    identity = _session_identity(request.headers.get("cookie")) or _basic_identity(request.headers.get("authorization"))
    if identity is None:
        # Only a wrong password counts. A request with no credentials, or a stale
        # cookie, is just "sign in": counting it would lock a shared (proxy) IP.
        if request.headers.get("authorization"):
            locked = _note_login_failure(client)
            if locked:
                return _locked_response(locked)
        return Response(
            "Login required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="On-call alert queue"'},
        )
    request.state.user = identity[0]
    request.state.role = identity[1]
    if identity[1] == "viewer" and request.method in UNSAFE_METHODS and request.url.path not in _ROLE_FREE_POSTS:
        return Response("Operator role required", status_code=403)
    if request.url.path in INGEST_PATHS:
        limited = _ingest_rate_response(request)
        if limited is not None:
            return limited
    response = await call_next(request)
    if request.url.path == "/logout" and request.method == "POST":
        _drop_session(request.headers.get("cookie"))
        response.delete_cookie(SESSION_COOKIE, path="/")
    return response


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
_AUDIT_FORMAT = logging.Formatter("%(asctime)s audit %(message)s")


def _audit_log_path() -> Path | None:
    """File copy of the audit log. AUDIT_LOG_PATH=off leaves stdout only."""
    raw = os.environ.get("AUDIT_LOG_PATH")
    if raw is not None and raw.strip().lower() in {"", "0", "off", "-"}:
        return None
    if raw and raw.strip():
        return Path(raw.strip())
    from product.ops import ops_path

    return ops_path().parent / "audit.log"


class _AuditFileHandler(logging.Handler):
    """Append-only audit file, opened on each record so tests can point it at a temp path."""

    def emit(self, record: logging.LogRecord) -> None:
        path = _audit_log_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _rotate_audit_log(path)
            with path.open("a", encoding="utf-8") as out:
                out.write(self.format(record) + "\n")
            os.chmod(path, 0o600)
        except OSError:
            return


def _rotate_audit_log(path: Path) -> None:
    """Keep one previous file when the log grows past AUDIT_LOG_MAX_BYTES (default 1 MB)."""
    max_bytes = _int_env("AUDIT_LOG_MAX_BYTES", 1_000_000)
    if max_bytes <= 0 or not path.exists() or path.stat().st_size < max_bytes:
        return
    rotated = path.with_name(path.name + ".1")
    try:
        rotated.unlink(missing_ok=True)
        path.replace(rotated)
    except OSError:
        return


def _ensure_audit_handlers() -> None:
    if not any(type(handler) is logging.StreamHandler for handler in AUDIT.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(_AUDIT_FORMAT)
        AUDIT.addHandler(stream)
    if not any(isinstance(handler, _AuditFileHandler) for handler in AUDIT.handlers):
        file_handler = _AuditFileHandler()
        file_handler.setFormatter(_AUDIT_FORMAT)
        AUDIT.addHandler(file_handler)
    AUDIT.setLevel(logging.INFO)


_ensure_audit_handlers()
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
        user = getattr(request.state, "user", None) or _login_user(request.headers.get("authorization")) or "-"
        AUDIT.info(
            "%s %s status=%s user=%s client=%s",
            request.method,
            request.url.path,
            response.status_code,
            user,
            login_client_id(request),
        )
    return response


router = APIRouter()


@router.post("/login")
async def login(request: Request):
    """One sign-in. The response sets an HttpOnly session cookie; later requests send that, not the password."""
    client = login_client_id(request)
    wait = _login_wait(client)
    if wait:
        return _locked_response(wait)
    identity = _basic_identity(request.headers.get("authorization"))
    if identity is None:
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            body = {}
        if not isinstance(body, dict):
            body = {}
        identity = _match_password(str(body.get("username") or ""), str(body.get("password") or ""))
    if identity is None:
        if not _accounts():
            return {"user": None, "role": "open"}
        locked = _note_login_failure(client)
        if locked:
            return _locked_response(locked)
        return Response("Login required", status_code=401)
    _note_login_success(client)
    token = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _SESSION_LOCK:
        for stale in [t for t, entry in _SESSIONS.items() if entry[2] <= now]:
            del _SESSIONS[stale]
        while len(_SESSIONS) >= MAX_SESSIONS:
            del _SESSIONS[min(_SESSIONS, key=lambda t: _SESSIONS[t][2])]
        _SESSIONS[token] = (identity[0], identity[1], now + SESSION_SECONDS)
    request.state.user = identity[0]
    response = JSONResponse({"user": identity[0], "role": identity[1]})
    _stamp_session(response, token, request)
    return response


@router.post("/logout")
async def logout() -> dict:
    return {"ok": True}
