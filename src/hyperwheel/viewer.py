"""Orthanc Explorer 2 over HTTPS, signed in with the Pi's own accounts. Runs as ``hyperwheel-viewer.service``.

People reach OE2 only through this proxy: from the hotspot at ``https://192.168.99.1:8443``, and on the Pi
itself at ``https://127.0.0.1:8443``, or ``http://127.0.0.1:8442`` for Chromium on the Pi's desktop. Traffic
on 127.0.0.1 never leaves the Pi, so it needs no encryption, and the browser shows no certificate warning there.
It:

- **signs people in** as ``datamanager`` or ``admin``, checking the password against the system's own hash
  (``/etc/shadow``, which the service may read through the ``shadow`` group), so no password is stored a second
  time. A password change on the Pi applies at the next sign-in. A sign-in gives a random session cookie, kept
  only in memory, for 8 hours or until the service restarts;
- **slows down wrong passwords** from each address (1, 2, 4 ... up to 30 seconds before the next try), and checks
  one password at a time, so guessing can't overload the Pi. Never a lockout;
- **lets nobody delete, modify or upload.** Only GET and HEAD go through, plus the three POSTs OE2 uses to search
  (``/tools/find``, ``/tools/lookup``, ``/tools/count-resources``). Everything else is refused here, whatever
  the page shows. Downloads (zip and DICOM files) are GETs, so they go through, and each is logged;
- **adds Orthanc's internal login** itself. Nobody ever sees it.

The certificate is made once on each Pi (``installer.viewer_certificate``). Browsers warn about it on the first
visit, because no public authority can sign one for 192.168.99.1. The password is still encrypted on the way.

Nothing typed into the sign-in page is logged, only the account (when it's one of ours) and the address.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.util
import hashlib
import hmac
import html
import http.client
import json
import logging
import re
import secrets
import socket
import ssl
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from . import passwords
from .installer import DATA_MANAGER, VIEWER_CERT, VIEWER_KEY
from .keys import KeyProblem, read_secret_json
from .release import ADMIN_USER

log = logging.getLogger("hyperwheel.viewer")

PORT = 8443
ADDRESSES = ("127.0.0.1", "192.168.99.1")
# Plain HTTP for the Pi's own browser, on the Pi only: 127.0.0.1 can't be reached from anywhere else.
LOCAL_PORT = 8442
ORTHANC = ("127.0.0.1", 8042)
ACCOUNTS = (DATA_MANAGER, ADMIN_USER)
SESSION_S = 8 * 3600
COOKIE = "hyperwheel_viewer"
LOGIN = "/hyperwheel/sign-in"
LOGOUT = "/hyperwheel/sign-out"
MAX_PASSWORD = 128
MAX_POST_BODY = 64 * 1024
FIRST_WAIT_S, LONGEST_WAIT_S = 1.0, 30.0
CHECK_QUEUE_S = 5.0

# OE2's searches. Each is a read. Anything else that isn't a GET or a HEAD changes something, so it's refused.
READ_ONLY_POSTS = ("/tools/find", "/tools/lookup", "/tools/count-resources")
DOWNLOAD = re.compile(r"/(patients|studies|series)/([0-9a-f-]{1,64})/archive|/instances/([0-9a-f-]{1,64})/file"
                      r"|/tools/create-archive")
LOOK_ONLY = "Orthanc Explorer 2 on this Pi is for looking and downloading only."

# Request headers passed on to Orthanc. Never the browser's cookies or its Authorization.
FORWARD = ("Accept", "Accept-Encoding", "Accept-Language", "Content-Type", "If-Modified-Since", "If-None-Match",
           "Range")
HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authenticate",
              "proxy-authorization", "set-cookie", "www-authenticate", "content-length"}


# ---- the password check ----------------------------------------------------------------------------------


class SystemHashes:
    """Checks a password against the system's hash for an account: yescrypt, SHA-512 or anything else the system's
    own ``libcrypt`` knows. Where there's no libcrypt (not a Pi), only SHA-512 hashes can be checked."""

    def __init__(self, shadow: Path = Path("/etc/shadow")):
        self.shadow = shadow
        name = ctypes.util.find_library("crypt")
        self._crypt = None
        if name and sys.platform.startswith("linux"):
            try:
                fn = ctypes.CDLL(name).crypt_rn
            except (OSError, AttributeError):
                fn = None
            if fn is not None:
                fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_int]
                fn.restype = ctypes.c_char_p
                self._crypt = fn

    def hash_of(self, account: str) -> str | None:
        """The account's hash, or None if it has none that a password could match (locked, empty, no account)."""
        try:
            lines = self.shadow.read_text().splitlines()
        except OSError as e:
            log.error("Viewer: the password hashes can't be read (%s).", e.strerror)
            return None
        for line in lines:
            fields = line.split(":")
            if len(fields) > 1 and fields[0] == account:
                hashed = fields[1]
                return hashed if hashed.startswith("$") else None
        return None

    def matches(self, password: str, hashed: str) -> bool:
        if self._crypt is not None:
            buffer = ctypes.create_string_buffer(32768)  # libxcrypt's struct crypt_data
            result = self._crypt(password.encode(), hashed.encode(), buffer, len(buffer))
            return result is not None and hmac.compare_digest(result, hashed.encode())
        return bool(passwords.HASH_PATTERN.fullmatch(hashed)) and passwords.matches(password, hashed)


# A hash checked when the account isn't one of ours or has no password, so the answer takes as long either way.
_NOBODY = None


def _nobody() -> str:
    global _NOBODY
    if _NOBODY is None:
        _NOBODY = passwords.sha512_crypt(secrets.token_hex(16))
    return _NOBODY


@dataclass
class Gate:
    """Who may sign in, and how fast wrong passwords may be tried."""
    hashes: SystemHashes = field(default_factory=SystemHashes)
    clock: Callable[[], float] = time.monotonic
    one_at_a_time: threading.BoundedSemaphore = field(default_factory=lambda: threading.BoundedSemaphore(1))
    failures: dict[str, tuple[int, float]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def wait_left(self, address: str) -> float:
        with self.lock:
            count, not_before = self.failures.get(address, (0, 0.0))
        return max(0.0, not_before - self.clock()) if count else 0.0

    def check(self, address: str, account: str, password: str) -> tuple[str, float]:
        """Returns ("ok" | "wrong" | "wait" | "busy", seconds to wait before the next try)."""
        left = self.wait_left(address)
        if left > 0:
            return "wait", left
        if not self.one_at_a_time.acquire(timeout=CHECK_QUEUE_S):
            return "busy", 0.0
        try:
            hashed = self.hashes.hash_of(account) if account in ACCOUNTS else None
            password = password[:MAX_PASSWORD]
            ok = self.hashes.matches(password, hashed or _nobody()) and hashed is not None
        finally:
            self.one_at_a_time.release()
        with self.lock:
            if ok:
                self.failures.pop(address, None)
                return "ok", 0.0
            count = self.failures.get(address, (0, 0.0))[0] + 1
            wait = min(FIRST_WAIT_S * 2 ** (count - 1), LONGEST_WAIT_S)
            self.failures[address] = (count, self.clock() + wait)
            if len(self.failures) > 1024:  # forget the oldest
                for old in sorted(self.failures, key=lambda a: self.failures[a][1])[:512]:
                    del self.failures[old]
        who = account if account in ACCOUNTS else "an account that isn't one of this Pi's"
        log.warning("Viewer: a wrong password for %s from %s (%d in a row).", who, address, count)
        return "wrong", wait


@dataclass
class Sessions:
    clock: Callable[[], float] = time.monotonic
    sessions: dict[str, tuple[str, float]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def start(self, account: str) -> str:
        token = secrets.token_urlsafe(32)
        with self.lock:
            now = self.clock()
            for key in [k for k, (_, ends) in self.sessions.items() if ends <= now]:
                del self.sessions[key]
            self.sessions[self._key(token)] = (account, now + SESSION_S)
        return token

    def account(self, token: str | None) -> str | None:
        if not token:
            return None
        with self.lock:
            found = self.sessions.get(self._key(token))
        if found is None or found[1] <= self.clock():
            return None
        return found[0]

    def end(self, token: str | None) -> None:
        if token:
            with self.lock:
                self.sessions.pop(self._key(token), None)


# ---- the requests ----------------------------------------------------------------------------------------


def allowed(method: str, path: str) -> bool:
    """Whether a request may go to Orthanc. Reads only: GET and HEAD, and OE2's searches."""
    if method in ("GET", "HEAD"):
        return True
    return method == "POST" and urlsplit(path).path in READ_ONLY_POSTS and "?" not in path


def safe_next(target: str) -> str:
    """Where to go after signing in: a path on this Pi, never another site."""
    if not target.startswith("/") or target.startswith("//") or "\\" in target or target.startswith(LOGIN):
        return "/ui/app/"
    return target


@dataclass
class Viewer:
    gate: Gate = field(default_factory=Gate)
    sessions: Sessions = field(default_factory=Sessions)
    orthanc: tuple[str, int] = ORTHANC
    orthanc_login: Callable[[], dict] = lambda: read_secret_json("orthanc", Path("/etc/hyperwheel/orthanc.json"))

    def authorization(self) -> str:
        login = self.orthanc_login()
        pair = f"{login['username']}:{login['password']}".encode()
        return "Basic " + base64.b64encode(pair).decode()

    def handler(self) -> type[BaseHTTPRequestHandler]:
        viewer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "Hyperwheel"
            sys_version = ""

            def log_message(self, *args):
                pass

            # -- helpers --

            def _token(self) -> str | None:
                for part in self.headers.get("Cookie", "").split(";"):
                    name, _, value = part.strip().partition("=")
                    if name == COOKIE:
                        return value
                return None

            def _send(self, status: int, body: bytes, ctype: str = "text/plain; charset=utf-8",
                      extra: dict[str, str] | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                for name, value in (extra or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _page(self, status: int, message: str = "", target: str = "/ui/app/") -> None:
                body = SIGN_IN_PAGE.replace("{message}", html.escape(message)).replace(
                    "{next}", html.escape(target, quote=True))
                self._send(status, body.encode(), "text/html; charset=utf-8",
                           {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                                       "form-action 'self'; frame-ancestors 'none'"})

            def _drain(self) -> bytes:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if not 0 <= length <= MAX_POST_BODY:
                    self.close_connection = True
                    return b""
                return self.rfile.read(length) if length else b""

            # -- routes --

            def do_GET(self):
                self._route()

            def do_HEAD(self):
                self._route()

            def do_POST(self):
                self._route()

            def do_PUT(self):
                self._route()

            def do_DELETE(self):
                self._route()

            def do_PATCH(self):
                self._route()

            def do_OPTIONS(self):
                self._route()

            def _route(self):
                path = urlsplit(self.path).path
                if not self.path.startswith("/"):
                    self._drain()
                    return self._send(400, b"Bad request")
                if path == LOGIN:
                    return self._sign_in()
                if path == LOGOUT:
                    self._drain()
                    viewer.sessions.end(self._token())
                    return self._send(303, b"", extra={"Location": LOGIN, "Set-Cookie": _cookie("", 0)})
                account = viewer.sessions.account(self._token())
                if account is None:
                    body = self._drain()
                    del body
                    if self.command == "GET" and "text/html" in self.headers.get("Accept", ""):
                        return self._send(303, b"", extra={"Location": f"{LOGIN}?next={quote(self.path, safe='')}"})
                    return self._send(401, json.dumps({"message": "Sign in again."}).encode(), "application/json")
                if not allowed(self.command, self.path):
                    self._drain()
                    log.info("Viewer: %s tried %s on %s, refused (look only).", account, self.command, path)
                    return self._send(403, LOOK_ONLY.encode())
                self._forward(account)

            def _sign_in(self):
                address = self.client_address[0]
                if self.command == "GET":
                    query = parse_qs(urlsplit(self.path).query)
                    return self._page(200, target=safe_next(query.get("next", ["/ui/app/"])[0]))
                if self.command != "POST":
                    self._drain()
                    return self._send(405, b"Method not allowed")
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/x-www-form-urlencoded":
                    self._drain()
                    return self._page(400, "Please sign in on this page.")
                form = parse_qs(self._drain().decode(errors="replace"))
                account = form.get("account", [""])[0].strip()
                password = form.get("password", [""])[0]
                target = safe_next(form.get("next", ["/ui/app/"])[0])
                result, wait = viewer.gate.check(address, account, password)
                del password, form
                if result == "ok":
                    token = viewer.sessions.start(account)
                    log.info("Viewer: %s signed in from %s.", account, address)
                    return self._send(303, b"", extra={"Location": target, "Set-Cookie": _cookie(token, SESSION_S)})
                if result == "busy":
                    return self._page(503, "The Pi is busy checking another sign-in. Try again in a moment.", target)
                seconds = max(1, round(wait))
                why = "That account and password don't match." if result == "wrong" else "Too many tries."
                return self._page(401 if result == "wrong" else 429,
                                  f"{why} Wait {seconds} second{'s' if seconds != 1 else ''} before you try again.",
                                  target)

            def _forward(self, account: str):
                body = self._drain() if self.command == "POST" else b""
                headers = {name: self.headers[name] for name in FORWARD if self.headers.get(name)}
                headers["Authorization"] = viewer.authorization()
                if self.command == "POST":
                    headers["Content-Length"] = str(len(body))
                upstream = http.client.HTTPConnection(*viewer.orthanc, timeout=600)
                try:
                    upstream.request(self.command, self.path, body=body or None, headers=headers)
                    response = upstream.getresponse()
                except OSError:
                    upstream.close()
                    return self._send(502, b"Orthanc isn't answering. It may be starting. Try again in a minute.")
                try:
                    found = DOWNLOAD.fullmatch(urlsplit(self.path).path)
                    if found and response.status == 200 and self.command == "GET":
                        log.info("Viewer: %s downloaded %s.", account, urlsplit(self.path).path)
                    self.send_response(response.status)
                    length = response.getheader("Content-Length")
                    for name, value in response.getheaders():
                        if name.lower() not in HOP_BY_HOP:
                            self.send_header(name, value)
                    chunked = length is None and self.command != "HEAD" and response.status not in (204, 304)
                    if chunked:
                        self.send_header("Transfer-Encoding", "chunked")
                    elif length is not None:
                        self.send_header("Content-Length", length)
                    self.send_header("X-Frame-Options", "SAMEORIGIN")
                    self.end_headers()
                    if self.command == "HEAD":
                        return
                    while True:
                        data = response.read(64 * 1024)
                        if not data:
                            break
                        if chunked:
                            self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
                        else:
                            self.wfile.write(data)
                    if chunked:
                        self.wfile.write(b"0\r\n\r\n")
                except (OSError, http.client.HTTPException):
                    self.close_connection = True  # the browser went away, or Orthanc did
                finally:
                    upstream.close()

        return Handler


def _cookie(token: str, max_age: int) -> str:
    return f"{COOKIE}={token}; Max-Age={max_age}; Path=/; HttpOnly; Secure; SameSite=Strict"


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def server_bind(self):
        # The hotspot's address may not be up yet when the service starts. Linux lets it be bound anyway.
        if hasattr(socket, "IP_FREEBIND"):
            self.socket.setsockopt(socket.IPPROTO_IP, socket.IP_FREEBIND, 1)
        super().server_bind()


def make_server(viewer: Viewer, address: str, port: int, context: ssl.SSLContext | None) -> Server:
    """``context`` None is plain HTTP, for tests only: the service always passes one."""
    server = Server((address, port), viewer.handler())
    if context is not None:
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def tls_context(cert: Path, key: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    return context


def fingerprint(cert: Path) -> str | None:
    """The certificate's SHA-256 fingerprint, as browsers show it, or None if there's no certificate."""
    try:
        der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    except (OSError, ValueError):
        return None
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def main(args: list[str]) -> int:
    import os
    if args:
        print("Usage: hyperwheel viewer")
        return 2
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    credentials = os.environ.get("CREDENTIALS_DIRECTORY")
    key = Path(credentials) / "viewer-key" if credentials else Path(VIEWER_KEY)
    try:
        context = tls_context(Path(VIEWER_CERT), key)
        viewer = Viewer()
        viewer.authorization()
    except (OSError, ssl.SSLError, KeyProblem, KeyError, ValueError) as e:
        log.error("Viewer: can't start (%s).", e)
        return 1
    servers = [make_server(viewer, address, PORT, context) for address in ADDRESSES]
    servers.append(make_server(viewer, "127.0.0.1", LOCAL_PORT, None))
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Viewer: Orthanc Explorer 2 is at https://%s:%d.", ADDRESSES[-1], PORT)
    servers[0].serve_forever()
    return 0


SIGN_IN_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in: Orthanc Explorer 2</title>
<style>
  body { margin:0; padding:16px; background:#f2f2f7; color:#111;
         font: 17px/1.4 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
  main { max-width: 420px; margin: 40px auto; background:#fff; border-radius:12px; padding:24px;
         box-shadow:0 1px 3px rgba(0,0,0,.06); }
  h1 { font-size:24px; margin:0 0 6px; }
  p { margin:0 0 14px; color:#6e6e73; }
  label { display:block; font-weight:600; margin:12px 0 4px; }
  input, select { width:100%; box-sizing:border-box; font-size:17px; padding:10px; border:1px solid #c7c7cc;
                  border-radius:8px; }
  button { margin-top:18px; font-size:17px; padding:10px 18px; border:0; border-radius:8px; background:#0066cc;
           color:#fff; }
  .message { color:#d70015; font-weight:600; }
</style>
</head>
<body>
<main>
  <h1>Orthanc Explorer 2</h1>
  <p>Sign in with this Pi's datamanager or admin account. You can look and download, but not change anything.</p>
  <p class="message">{message}</p>
  <form method="post" action="/hyperwheel/sign-in">
    <input type="hidden" name="next" value="{next}">
    <label for="account">Account</label>
    <select id="account" name="account">
      <option value="datamanager">datamanager</option>
      <option value="admin">admin</option>
    </select>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" autofocus>
    <button type="submit">Sign in</button>
  </form>
</main>
</body>
</html>
"""
