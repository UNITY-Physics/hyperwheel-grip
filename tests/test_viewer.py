"""Orthanc Explorer 2 behind the viewer: the Pi's own accounts, slowed-down guessing, and look and download only."""

from __future__ import annotations

import base64
import ctypes
import ctypes.util
import http.client
import json
import logging
import shutil
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

import pytest

from hyperwheel import cli, passwords, viewer
from hyperwheel.installer import UNITS, Installer, Layout, orthanc_json, run_command
from hyperwheel.viewer import Gate, Sessions, SystemHashes, Viewer, allowed, safe_next

DM_PW, ADMIN_PW = "borak-kefim-tumos", "dobun-hasip-zerol"
ORTHANC_LOGIN = {"username": "hyperwheel", "password": "internal-orthanc-pw"}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def shadow(tmp_path) -> Path:
    path = tmp_path / "shadow"
    path.write_text(
        "root:*:20000:0:99999:7:::\n"
        f"datamanager:{passwords.sha512_crypt(DM_PW, rounds=1000)}:20000:0:99999:7:::\n"
        f"admin:{passwords.sha512_crypt(ADMIN_PW, rounds=1000)}:20000:0:99999:7:::\n"
        f"hyperwheel:!{passwords.sha512_crypt('x' * 12, rounds=1000)}:20000::::::\n"
        f"pi:{passwords.sha512_crypt('raspberry', rounds=1000)}:20000:0:99999:7:::\n")
    return path


# ---- passwords ------------------------------------------------------------------------------------------


def test_hashes_are_read_from_the_system_and_locked_ones_match_nothing(shadow):
    hashes = SystemHashes(shadow)
    assert hashes.hash_of("datamanager").startswith("$6$rounds=1000$")
    assert hashes.hash_of("root") is None and hashes.hash_of("hyperwheel") is None
    assert hashes.hash_of("nobody") is None
    assert hashes.matches(DM_PW, hashes.hash_of("datamanager"))
    assert not hashes.matches(ADMIN_PW, hashes.hash_of("datamanager"))


@pytest.mark.skipif(not (sys.platform.startswith("linux") and ctypes.util.find_library("crypt")),
                    reason="the system's libcrypt (on the Pi and in CI)")
def test_the_systems_own_yescrypt_hashes_are_checked_too(tmp_path):
    lib = ctypes.CDLL(ctypes.util.find_library("crypt"))
    lib.crypt_gensalt_rn.restype = ctypes.c_char_p
    lib.crypt_gensalt_rn.argtypes = [ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p, ctypes.c_int,
                                     ctypes.c_char_p, ctypes.c_int]
    lib.crypt_rn.restype = ctypes.c_char_p
    lib.crypt_rn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_int]
    salt_buffer = ctypes.create_string_buffer(128)
    salt = lib.crypt_gensalt_rn(b"$y$", 0, None, 0, salt_buffer, len(salt_buffer))
    data = ctypes.create_string_buffer(32768)
    yescrypt = lib.crypt_rn(DM_PW.encode(), salt, data, len(data)).decode()
    assert yescrypt.startswith("$y$")
    (tmp_path / "shadow").write_text(f"datamanager:{yescrypt}:20000:0:99999:7:::\n")
    hashes = SystemHashes(tmp_path / "shadow")
    assert hashes.matches(DM_PW, yescrypt) and not hashes.matches("wrong-wrong-wrong", yescrypt)
    assert hashes.matches(ADMIN_PW, passwords.sha512_crypt(ADMIN_PW, rounds=1000))


# ---- slowing down guessing --------------------------------------------------------------------------------


@pytest.fixture
def gate(shadow):
    return Gate(hashes=SystemHashes(shadow), clock=Clock())


def test_only_datamanager_and_admin_may_sign_in(gate):
    assert gate.check("a", "datamanager", DM_PW) == ("ok", 0.0)
    assert gate.check("b", "admin", ADMIN_PW) == ("ok", 0.0)
    assert gate.check("c", "pi", "raspberry")[0] == "wrong"  # another account on the Pi, with the right password
    assert gate.check("d", "root", "")[0] == "wrong"
    assert gate.check("e", "hyperwheel", "x" * 12)[0] == "wrong"


def test_wrong_passwords_wait_longer_each_time_up_to_30_seconds_never_a_lockout(gate, caplog):
    waits = []
    with caplog.at_level(logging.INFO):
        for _ in range(7):
            result, wait = gate.check("192.168.99.20", "admin", "guess-guess-guess")
            assert result == "wrong"
            assert gate.check("192.168.99.20", "admin", ADMIN_PW)[0] == "wait"  # not even checked yet
            waits.append(wait)
            gate.clock.now += wait
    assert waits == [1, 2, 4, 8, 16, 30, 30]
    assert gate.check("192.168.99.21", "admin", ADMIN_PW)[0] == "ok"  # another address isn't held up
    assert gate.check("192.168.99.20", "admin", ADMIN_PW)[0] == "ok"
    assert gate.check("192.168.99.20", "admin", "guess-guess-guess")[1] == 1  # a sign-in starts it again
    assert "guess" not in caplog.text and "wrong password for admin from 192.168.99.20 (7 in a row)" in caplog.text


def test_a_name_that_isnt_an_account_is_never_logged(gate, caplog):
    with caplog.at_level(logging.INFO):
        gate.check("a", DM_PW, DM_PW)  # a password typed into the account box
    assert DM_PW not in caplog.text and "an account that isn't one of this Pi's" in caplog.text


def test_one_check_at_a_time(gate):
    gate.one_at_a_time.acquire()
    try:
        viewer.CHECK_QUEUE_S, saved = 0.01, viewer.CHECK_QUEUE_S
        assert gate.check("a", "admin", ADMIN_PW) == ("busy", 0.0)
    finally:
        viewer.CHECK_QUEUE_S = saved
        gate.one_at_a_time.release()


def test_sessions_end_after_8_hours_or_when_signed_out():
    clock = Clock()
    sessions = Sessions(clock=clock)
    token = sessions.start("datamanager")
    assert sessions.account(token) == "datamanager" and sessions.account("forged") is None
    assert token not in sessions.sessions  # only its hash is kept
    clock.now += 8 * 3600
    assert sessions.account(token) is None
    other = sessions.start("admin")
    sessions.end(other)
    assert sessions.account(other) is None


# ---- what goes through ------------------------------------------------------------------------------------


@pytest.mark.parametrize("method,path", [
    ("GET", "/ui/app/"), ("GET", "/studies/abc/archive"), ("GET", "/instances/abc/file"), ("HEAD", "/system"),
    ("GET", "/tools/create-archive?resources=a,b"), ("POST", "/tools/find"), ("POST", "/tools/lookup"),
    ("POST", "/tools/count-resources")])
def test_reading_and_downloading_go_through(method, path):
    assert allowed(method, path)


@pytest.mark.parametrize("method,path", [
    ("DELETE", "/studies/abc"), ("POST", "/instances"), ("POST", "/studies/abc/modify"),
    ("POST", "/studies/abc/anonymize"), ("POST", "/tools/bulk-delete"), ("POST", "/tools/execute-script"),
    ("PUT", "/studies/abc/labels/x"), ("PUT", "/tools/log-level"), ("POST", "/modalities/x/store"),
    ("POST", "/tools/find/../execute-script"), ("POST", "/tools/find?x=1"), ("POST", "/tools/reset"),
    ("PATCH", "/studies/abc"), ("OPTIONS", "/")])
def test_anything_that_changes_something_is_refused(method, path):
    assert not allowed(method, path)


def test_after_signing_in_it_goes_only_to_this_pi():
    assert safe_next("/ui/app/#/study?id=1") == "/ui/app/#/study?id=1"
    for target in ("https://elsewhere.example", "//elsewhere.example/x", "/\\elsewhere", "ui/app",
                   "/hyperwheel/sign-in"):
        assert safe_next(target) == "/ui/app/"


def test_oe2_hides_what_changes_anything_and_keeps_downloads():
    ui = orthanc_json([])["OrthancExplorer2"]["UiOptions"]
    for option in ("EnableUpload", "EnableDeleteResources", "EnableModification", "EnableAnonymization",
                   "EnableSendTo", "EnableEditLabels", "EnableAddSeries", "EnableSettings"):
        assert ui[option] is False
    assert ui["EnableDownloadZip"] and ui["EnableDownloadDicomFile"]


def test_the_service_can_check_passwords_and_nothing_more():
    unit = UNITS["hyperwheel-viewer.service"]
    assert "DynamicUser=yes" in unit and "SupplementaryGroups=shadow" in unit
    assert "LoadCredential=viewer-key:/etc/hyperwheel/viewer/key.pem" in unit
    assert "InaccessiblePaths=-/var/lib/hyperwheel -/etc/hyperwheel/keys" in unit
    assert cli.problem(["viewer"]) is None and "viewer" not in cli.help_text(True, False)


# ---- over HTTP, with a stand-in for Orthanc ---------------------------------------------------------------


class FakeOrthanc(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.requests: list[tuple[str, str, dict, bytes]] = []
        orthanc = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _any(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                orthanc.requests.append((self.command, self.path, dict(self.headers), body))
                if self.path.endswith("/archive"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/zip")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    for _ in range(40):
                        chunk = b"z" * 50_000
                        self.wfile.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                    return
                out = json.dumps({"path": self.path}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.send_header("Set-Cookie", "orthanc=should-not-pass")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(out)

            do_GET = do_POST = do_HEAD = do_DELETE = do_PUT = _any

        super().__init__(("127.0.0.1", 0), Handler)


@pytest.fixture
def site(shadow):
    orthanc = FakeOrthanc()
    threading.Thread(target=orthanc.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    v = Viewer(gate=Gate(hashes=SystemHashes(shadow), clock=Clock()), sessions=Sessions(),
               orthanc=orthanc.server_address, orthanc_login=lambda: ORTHANC_LOGIN)
    server = viewer.make_server(v, "127.0.0.1", 0, None)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield server.server_address[1], orthanc
    server.shutdown()
    orthanc.shutdown()


def call(port, method, path, *, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r, data


def sign_in(port, account="datamanager", password=DM_PW, target="/ui/app/"):
    return call(port, "POST", "/hyperwheel/sign-in", body=urlencode({"account": account, "password": password,
                                                                      "next": target}),
                headers={"Content-Type": "application/x-www-form-urlencoded"})


def cookie_of(r) -> str:
    return r.getheader("Set-Cookie").split(";")[0]


def test_nothing_reaches_orthanc_without_signing_in(site):
    port, orthanc = site
    r, _ = call(port, "GET", "/ui/app/#x", headers={"Accept": "text/html"})
    assert r.status == 303 and r.getheader("Location").startswith("/hyperwheel/sign-in?next=%2Fui%2Fapp%2F")
    r, _ = call(port, "GET", "/studies", headers={"Accept": "application/json"})
    assert r.status == 401
    r, _ = call(port, "DELETE", "/studies/abc")
    assert r.status == 401
    r, _ = call(port, "GET", "/studies", headers={"Cookie": "hyperwheel_viewer=forged"})
    assert r.status == 401
    assert orthanc.requests == []


def test_the_sign_in_page(site):
    port, _ = site
    r, page = call(port, "GET", "/hyperwheel/sign-in?next=%2Fui%2Fapp%2F%23%2Fstudies")
    assert r.status == 200 and b'value="/ui/app/#/studies"' in page and b"datamanager or admin" in page
    assert "default-src 'none'" in r.getheader("Content-Security-Policy")
    r, page = call(port, "GET", '/hyperwheel/sign-in?next=%22%3E%3Cscript%3E')
    assert b"<script>" not in page


def test_a_wrong_password_says_how_long_to_wait_and_gives_no_cookie(site):
    port, _ = site
    r, page = sign_in(port, password="wrong-wrong-wrong")
    assert r.status == 401 and r.getheader("Set-Cookie") is None
    assert b"That account and password don&#x27;t match. Wait 1 second before you try again." in page
    r, page = sign_in(port)  # straight away, even with the right password
    assert r.status == 429 and b"Too many tries." in page


def test_signed_in_it_reads_and_downloads_with_orthancs_own_login(site, caplog):
    port, orthanc = site
    r, _ = sign_in(port, target="/ui/app/#/studies")
    assert r.status == 303 and r.getheader("Location") == "/ui/app/#/studies"
    set_cookie = r.getheader("Set-Cookie")
    for flag in ("HttpOnly", "Secure", "SameSite=Strict", "Max-Age=28800"):
        assert flag in set_cookie
    cookie = {"Cookie": cookie_of(r)}

    r, data = call(port, "GET", "/studies?expand", headers={**cookie, "Authorization": "Basic Zm9vOmJhcg=="})
    assert r.status == 200 and json.loads(data) == {"path": "/studies?expand"}
    assert r.getheader("Set-Cookie") is None  # nothing of Orthanc's is passed back
    method, path, headers, _ = orthanc.requests[-1]
    expected = "Basic " + base64.b64encode(b"hyperwheel:internal-orthanc-pw").decode()
    assert headers["Authorization"] == expected and "Cookie" not in headers

    r, _ = call(port, "POST", "/tools/find", body=b'{"Level": "Study", "Query": {}}',
                headers={**cookie, "Content-Type": "application/json"})
    assert r.status == 200 and orthanc.requests[-1][3] == b'{"Level": "Study", "Query": {}}'

    with caplog.at_level(logging.INFO):
        r, data = call(port, "GET", "/studies/0123abcd-ef01/archive", headers=cookie)
    assert r.status == 200 and len(data) == 40 * 50_000
    assert "datamanager downloaded /studies/0123abcd-ef01/archive" in caplog.text

    r, _ = call(port, "HEAD", "/system", headers=cookie)
    assert r.status == 200


def test_signed_in_nothing_can_be_changed(site):
    port, orthanc = site
    cookie = {"Cookie": cookie_of(sign_in(port, "admin", ADMIN_PW)[0])}
    before = len(orthanc.requests)
    for method, path in (("DELETE", "/studies/abc"), ("POST", "/instances"), ("POST", "/studies/abc/modify"),
                         ("PUT", "/tools/log-level"), ("POST", "/tools/bulk-delete")):
        r, data = call(port, method, path, body=b"{}", headers=cookie)
        assert r.status == 403 and b"for looking and downloading only" in data
    assert len(orthanc.requests) == before


def test_signing_out_ends_the_session(site):
    port, orthanc = site
    cookie = {"Cookie": cookie_of(sign_in(port)[0])}
    r, _ = call(port, "GET", "/hyperwheel/sign-out", headers=cookie)
    assert r.status == 303 and "Max-Age=0" in r.getheader("Set-Cookie")
    assert call(port, "GET", "/studies", headers=cookie)[0].status == 401


def test_orthanc_not_answering_is_said_plainly(shadow):
    v = Viewer(gate=Gate(hashes=SystemHashes(shadow), clock=Clock()), orthanc=("127.0.0.1", 1),
               orthanc_login=lambda: ORTHANC_LOGIN)
    server = viewer.make_server(v, "127.0.0.1", 0, None)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        port = server.server_address[1]
        r, data = call(port, "GET", "/studies", headers={"Cookie": cookie_of(sign_in(port)[0])})
        assert r.status == 502 and b"Orthanc isn't answering" in data
    finally:
        server.shutdown()


# ---- HTTPS, with this Pi's own certificate ----------------------------------------------------------------


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_https_with_the_pis_certificate_and_its_fingerprint(tmp_path, shadow):
    Installer(Path("."), Path("."), Layout(tmp_path), run=run_command, chown=lambda *a: None).viewer_certificate()
    cert, key = tmp_path / "etc/hyperwheel/viewer/cert.pem", tmp_path / "etc/hyperwheel/viewer/key.pem"
    v = Viewer(gate=Gate(hashes=SystemHashes(shadow), clock=Clock()), orthanc_login=lambda: ORTHANC_LOGIN)
    server = viewer.make_server(v, "127.0.0.1", 0, viewer.tls_context(cert, key))
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        client = ssl.create_default_context(cafile=str(cert))
        conn = http.client.HTTPSConnection("127.0.0.1", server.server_address[1], context=client, timeout=10)
        conn.request("GET", "/hyperwheel/sign-in")
        assert conn.getresponse().status == 200
        der = conn.sock.getpeercert(binary_form=True)
        conn.close()
        old = ssl.create_default_context(cafile=str(cert))
        old.maximum_version = ssl.TLSVersion.TLSv1_1
        old.minimum_version = ssl.TLSVersion.TLSv1
        with pytest.raises((ssl.SSLError, OSError)):
            http.client.HTTPSConnection("127.0.0.1", server.server_address[1], context=old, timeout=10).request(
                "GET", "/")
    finally:
        server.shutdown()
    import hashlib
    expected = hashlib.sha256(der).hexdigest().upper()
    assert viewer.fingerprint(cert).replace(":", "") == expected
    assert viewer.fingerprint(tmp_path / "missing.pem") is None
